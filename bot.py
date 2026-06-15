import os
import sys
import subprocess
import asyncio
import tempfile
import time
import json
import re
import base64 as _b64
from pathlib import Path
from datetime import datetime

# ── Install Playwright Chromium at runtime ────────────────────
print("Ensuring Playwright Chromium is installed...")
subprocess.run(["playwright", "install", "chromium", "--with-deps"],
               check=False, capture_output=False)
print("Playwright ready.")

import pdfplumber
import fitz
from deep_translator import GoogleTranslator
from playwright.async_api import async_playwright

try:
    import pytesseract
    from PIL import Image
    import io as _io
    OCR_AVAILABLE = True
    print("OCR (pytesseract) ✅")
except ImportError:
    OCR_AVAILABLE = False
    print("OCR not available")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

TOKEN   = os.getenv("TELEGRAM_TOKEN", "8767341498:AAEFyEg5aHmAs4hCy1E60xsCfCZtQ3fFbfA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ═══════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════
processing_semaphore = asyncio.Semaphore(2)        # max 2 concurrent
user_cancel_events:  dict[int, asyncio.Event] = {}
user_pending_pdf:    dict[int, dict]          = {}  # path / filename / total
waiting_for_range:   set[int]                 = set()

# ═══════════════════════════════════════════════════════════
#  Stats
# ═══════════════════════════════════════════════════════════
STATS_FILE = Path(__file__).parent / "stats.json"

def load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return {"total_pdfs": 0, "total_pages": 0, "users": [], "today": {}}

def save_stats(s: dict):
    s2 = dict(s)
    s2["users"] = list(set(s2.get("users", [])))
    STATS_FILE.write_text(json.dumps(s2, indent=2))

_stats = load_stats()

def track_usage(user_id: int, pages: int):
    _stats["total_pdfs"] = _stats.get("total_pdfs", 0) + 1
    _stats["total_pages"] = _stats.get("total_pages", 0) + pages
    users = _stats.get("users", [])
    if user_id not in users:
        users.append(user_id)
    _stats["users"] = users
    today = datetime.now().strftime("%Y-%m-%d")
    _stats.setdefault("today", {})
    _stats["today"][today] = _stats["today"].get(today, 0) + 1
    save_stats(_stats)

# ═══════════════════════════════════════════════════════════
#  Myanmar Font (base64 embed)
# ═══════════════════════════════════════════════════════════
FONT_PATH = Path(__file__).parent / "fonts" / "MyanmarText.ttf"

def _get_font_b64() -> str:
    if FONT_PATH.exists():
        return _b64.b64encode(FONT_PATH.read_bytes()).decode()
    return ""

_FONT_B64 = _get_font_b64()

def _font_face_css() -> str:
    if _FONT_B64:
        return f"""@font-face {{
    font-family: 'MyanmarText';
    src: url('data:font/truetype;base64,{_FONT_B64}') format('truetype');
    font-weight: normal;
  }}"""
    return ""

# ═══════════════════════════════════════════════════════════
#  Translation
# ═══════════════════════════════════════════════════════════
def translate_text(text: str) -> str:
    if not text.strip():
        return ""
    translator = GoogleTranslator(source="en", target="my")
    chunks = [text[i:i+4500] for i in range(0, len(text), 4500)]
    result = []
    for chunk in chunks:
        try:
            result.append(translator.translate(chunk))
            time.sleep(0.3)
        except Exception:
            result.append(chunk)
    return " ".join(result)

# ═══════════════════════════════════════════════════════════
#  OCR (image-based PDF fallback)
# ═══════════════════════════════════════════════════════════
def ocr_page(fitz_page) -> str:
    if not OCR_AVAILABLE:
        return ""
    try:
        mat = fitz.Matrix(150/72, 150/72)
        pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
        img = Image.open(_io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang="eng").strip()
    except Exception:
        return ""

# ═══════════════════════════════════════════════════════════
#  PDF Processing
# ═══════════════════════════════════════════════════════════
async def process_pdf(
    pdf_path: str,
    page_range: tuple[int, int] | None,
    progress_cb=None,
    cancel_event: asyncio.Event | None = None
) -> dict[int, str]:
    translations = {}
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        s, e = (page_range[0]-1, page_range[1]) if page_range else (0, total)
        pages = list(range(s, min(e, total)))
        count = len(pages)
        fitz_doc = fitz.open(pdf_path)

        for idx, i in enumerate(pages):
            if cancel_event and cancel_event.is_set():
                break
            pn  = i + 1
            prog = idx + 1
            if progress_cb and (prog == 1 or prog % 5 == 0 or prog == count):
                bar = "▓" * int(prog/count*20) + "░" * (20 - int(prog/count*20))
                await progress_cb(
                    f"🌐 ဘာသာပြန်နေပါတယ် — {prog}/{count} မျက်နှာ\n"
                    f"{bar} {int(prog/count*100)}%"
                )
            text = pdf.pages[i].extract_text() or ""
            if not text.strip():
                text = ocr_page(fitz_doc[i])          # OCR fallback
            if text.strip():
                translations[pn] = translate_text(text)
            else:
                translations[pn] = "(ဤစာမျက်နှာတွင် ဘာသာပြန်မရရှိပါ)"

        fitz_doc.close()
    return translations

# ═══════════════════════════════════════════════════════════
#  HTML Builder
# ═══════════════════════════════════════════════════════════
def build_html(page_num: int, total: int, img_b64: str, myan_text: str) -> str:
    safe = (myan_text
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br>"))
    fc = _font_face_css()
    ff = ("'MyanmarText','Myanmar Text','Noto Sans Myanmar',sans-serif"
          if _FONT_B64 else "'Myanmar Text','Noto Sans Myanmar',sans-serif")
    return f"""<!DOCTYPE html>
<html lang="my"><head><meta charset="UTF-8">
<style>
  {fc}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:1123px; height:794px; font-family:{ff};
    background:#f0f4ff; display:flex; flex-direction:column; overflow:hidden; }}
  .header {{ background:#1a1a2e; color:white; padding:5px 16px; font-size:11px;
    display:flex; justify-content:space-between; font-family:Arial,sans-serif; flex-shrink:0; }}
  .main {{ display:flex; flex:1; overflow:hidden; }}
  .left {{ width:50%; background:white; display:flex; flex-direction:column;
    border-right:3px solid #3b82f6; }}
  .left-label {{ background:#1e3a6e; color:white; font-family:Arial,sans-serif;
    font-size:10px; text-align:center; padding:4px; flex-shrink:0; }}
  .left-img {{ flex:1; display:flex; justify-content:center; align-items:center;
    padding:8px; overflow:hidden; }}
  .left-img img {{ max-width:100%; max-height:100%; object-fit:contain; }}
  .right {{ width:50%; background:#fff; display:flex; flex-direction:column; overflow:hidden; }}
  .right-label {{ background:#dbeafe; color:#1e3a6e; font-size:12px; font-family:{ff};
    padding:5px 14px; border-bottom:2px solid #93c5fd; flex-shrink:0; }}
  .translation {{ flex:1; padding:12px 16px; font-size:14px; line-height:2.2;
    color:#0d1b2a; overflow:hidden; word-break:break-word; font-family:{ff}; }}
  .footer {{ background:#dbeafe; font-family:Arial,sans-serif; font-size:8px;
    color:#6b7280; text-align:center; padding:3px; flex-shrink:0; }}
</style></head>
<body>
  <div class="header">
    <span>Myanmar PDF Translator &nbsp;|&nbsp; Page {page_num}/{total}</span>
    <span>မြန်မာဘာသာပြန်</span>
  </div>
  <div class="main">
    <div class="left">
      <div class="left-label">Original — Page {page_num}</div>
      <div class="left-img"><img src="data:image/jpeg;base64,{img_b64}"></div>
    </div>
    <div class="right">
      <div class="right-label">မြန်မာဘာသာပြန်ချက် — စာမျက်နှာ {page_num}</div>
      <div class="translation">{safe}</div>
    </div>
  </div>
  <div class="footer">Myanmar PDF Translator Bot &nbsp;|&nbsp; Myanmar Text Font</div>
</body></html>"""

# ═══════════════════════════════════════════════════════════
#  Playwright Render
# ═══════════════════════════════════════════════════════════
async def html_to_jpg(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1123, "height": 794})
        await page.set_content(html, wait_until="domcontentloaded", timeout=15000)
        jpg = await page.screenshot(
            clip={"x":0,"y":0,"width":1123,"height":794},
            type="jpeg", quality=82
        )
        await browser.close()
    return jpg

def jpgs_to_pdf(jpg_list: list[bytes]) -> bytes:
    doc = fitz.open()
    for jpg in jpg_list:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(jpg); tmp = f.name
        idoc = fitz.open(tmp)
        ipdf = fitz.open("pdf", idoc.convert_to_pdf())
        idoc.close()
        doc.insert_pdf(ipdf)
        os.unlink(tmp)
    out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc.save(out.name, deflate=True, garbage=4, clean=True)
    doc.close()
    with open(out.name, "rb") as f: data = f.read()
    os.unlink(out.name)
    return data

# ═══════════════════════════════════════════════════════════
#  Full Pipeline
# ═══════════════════════════════════════════════════════════
async def make_bilingual_pdf(
    pdf_path: str,
    page_range: tuple[int, int] | None,
    progress_cb=None,
    cancel_event: asyncio.Event | None = None
) -> bytes:
    pdf_doc = fitz.open(pdf_path)
    total   = len(pdf_doc)
    s, e    = (page_range[0]-1, page_range[1]) if page_range else (0, total)
    pages   = list(range(s, min(e, total)))

    if progress_cb:
        await progress_cb(
            f"📄 ဘာသာပြန်နေပါတယ် ({len(pages)} မျက်နှာ)...\n⏳ ခဏစောင့်ပါ"
        )

    translations = await process_pdf(pdf_path, page_range, progress_cb, cancel_event)
    if cancel_event and cancel_event.is_set():
        pdf_doc.close(); return b""

    count = len(pages)
    jpgs  = []
    for idx, i in enumerate(pages):
        if cancel_event and cancel_event.is_set(): break
        pn   = i + 1
        prog = idx + 1
        if progress_cb and (prog == 1 or prog % 5 == 0 or prog == count):
            bar = "▓" * int(prog/count*20) + "░" * (20 - int(prog/count*20))
            await progress_cb(
                f"✅ ဘာသာပြန်ပြီး!\n🎨 PDF ဆောက်နေပါတယ် — {prog}/{count} မျက်နှာ\n"
                f"{bar} {int(prog/count*100)}%"
            )
        page = pdf_doc[i]
        mat  = fitz.Matrix(96/72, 96/72)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        b64  = _b64.b64encode(pix.tobytes("jpeg", jpg_quality=85)).decode()
        myan = translations.get(pn, "(ဘာသာပြန်မရရှိ)")
        html = build_html(pn, total, b64, myan)
        jpgs.append(await html_to_jpg(html))

    pdf_doc.close()
    if not jpgs: return b""
    if progress_cb: await progress_cb("📦 PDF စုစည်းနေပါတယ်...")
    return jpgs_to_pdf(jpgs)

# ═══════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════
def cancel_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{user_id}")
    ]])

def pages_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 All Pages",    callback_data="pages_all"),
        InlineKeyboardButton("📑 Select Range", callback_data="pages_range"),
    ]])

# ═══════════════════════════════════════════════════════════
#  Core Processing Task
# ═══════════════════════════════════════════════════════════
async def _run(msg, user_id: int, pending: dict, page_range: tuple | None):
    cancel_event = asyncio.Event()
    user_cancel_events[user_id] = cancel_event

    async with processing_semaphore:
        if cancel_event.is_set():
            try: await msg.edit_text("❌ Cancel လုပ်ပြီးပါပြီ")
            except: pass
            return

        pdf_path = pending["path"]
        filename = pending["filename"]
        total_pg = pending["total"]

        async def progress(text: str):
            try: await msg.edit_text(text, reply_markup=cancel_kb(user_id))
            except: pass

        try:
            pdf_data = await make_bilingual_pdf(
                pdf_path, page_range, progress_cb=progress, cancel_event=cancel_event
            )

            if cancel_event.is_set() or not pdf_data:
                await msg.edit_text("❌ Cancel လုပ်ပြီးပါပြီ")
                return

            pages_done = (page_range[1] - page_range[0] + 1) if page_range else total_pg
            track_usage(user_id, pages_done)

            await msg.edit_text("📤 PDF ပေးပို့နေပါတယ်...")
            await msg.reply_document(
                document=pdf_data,
                filename=f"myanmar_{filename}",
                caption="✅ မြန်မာဘာသာပြန် PDF ပြီးပါပြီ!\nဘယ် = မူရင်း | ညာ = မြန်မာ"
            )
            await msg.delete()

        except Exception as ex:
            try: await msg.edit_text(f"❌ Error: {str(ex)[:200]}")
            except: pass
        finally:
            user_cancel_events.pop(user_id, None)
            if os.path.exists(pdf_path):
                os.unlink(pdf_path)

# ═══════════════════════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! 👋\n\n"
        "📄 PDF ဖိုင်တစ်ခု ပို့လိုက်ပါ\n"
        "➡️ မြန်မာဘာသာပြန်ပြီး PDF ပြန်ပေးပါမယ်\n\n"
        f"🔍 OCR: {'✅ On (image PDF support)' if OCR_AVAILABLE else '❌ Off'}\n"
        "👥 Max 2 users တစ်ချိန်ထဲ process လုပ်နိုင်\n"
        "⚠️ ကြာနိုင်ပါသည် (2–5 min)"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 အသုံးပြုနည်း\n\n"
        "1️⃣ PDF ဖိုင် ပို့ပါ\n"
        "2️⃣ All Pages / Range ရွေးပါ\n"
        "3️⃣ ❌ Cancel button နဲ့ ဖျက်လို့ ရပါတယ်\n\n"
        "📌 Layout: ဘယ် = မူရင်း | ညာ = မြန်မာ\n"
        f"🔍 OCR: {'✅ On' if OCR_AVAILABLE else '❌ Off'}\n"
        "📏 Max: 20MB"
    )

async def handle_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc     = update.message.document
    user_id = update.effective_user.id

    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ PDF ဖိုင်သာ လက်ခံပါသည်")
        return
    if doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ ဖိုင် 20MB ထက်ကြီးနေပါသည်")
        return
    if user_id in user_cancel_events and not user_cancel_events[user_id].is_set():
        await update.message.reply_text("⏳ ယခု processing ဆဲ ရှိပါတယ်")
        return

    msg  = await update.message.reply_text("⏳ PDF ဒေါင်းလုဒ်လုပ်နေပါတယ်...")
    tmp  = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    file = await ctx.bot.get_file(doc.file_id)
    await file.download_to_drive(tmp.name)

    pdf_doc = fitz.open(tmp.name)
    total   = len(pdf_doc)
    pdf_doc.close()

    user_pending_pdf[user_id] = {
        "path": tmp.name, "filename": doc.file_name, "total": total
    }

    await msg.edit_text(
        f"📄 *{doc.file_name}*\n📑 {total} မျက်နှာ\n\nမည်သည့် မျက်နှာများ ဘာသာပြန်မလဲ?",
        reply_markup=pages_kb(),
        parse_mode="Markdown"
    )

async def cb_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pending = user_pending_pdf.get(user_id)

    if not pending:
        await query.edit_message_text("❌ PDF ရှိမတွေ့ပါ။ ထပ်ပို့ပါ")
        return

    if query.data == "pages_all":
        waiting_for_range.discard(user_id)
        user_pending_pdf.pop(user_id, None)
        await query.edit_message_text(
            "⏳ Queue မှာ စောင့်နေပါတယ်...",
            reply_markup=cancel_kb(user_id)
        )
        asyncio.create_task(_run(query.message, user_id, pending, None))

    elif query.data == "pages_range":
        waiting_for_range.add(user_id)
        await query.edit_message_text(
            f"📑 {pending['total']} မျက်နှာ ရှိပါတယ်\n\n"
            "Range ပေးပါ (ဥပမာ: `1-10`)",
            parse_mode="Markdown"
        )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in waiting_for_range:
        return

    text  = update.message.text.strip()
    match = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", text)
    if not match:
        await update.message.reply_text("❌ Format မမှန်ပါ — ဥပမာ: `1-10`", parse_mode="Markdown")
        return

    start, end = int(match.group(1)), int(match.group(2))
    pending    = user_pending_pdf.get(user_id)

    if not pending:
        await update.message.reply_text("❌ PDF ရှိမတွေ့ပါ")
        waiting_for_range.discard(user_id)
        return

    total = pending["total"]
    if start < 1 or end > total or start > end:
        await update.message.reply_text(f"❌ Range မမှန်ပါ (1–{total} ကြားဖြစ်ရပါမယ်)")
        return

    waiting_for_range.discard(user_id)
    user_pending_pdf.pop(user_id, None)
    msg = await update.message.reply_text(
        f"⏳ Queue မှာ စောင့်နေပါတယ်... ({start}–{end} မျက်နှာ)",
        reply_markup=cancel_kb(user_id)
    )
    asyncio.create_task(_run(msg, user_id, pending, (start, end)))

async def cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id

    try:
        target_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid", show_alert=True)
        return

    if user_id != target_id:
        await query.answer("❌ သင့် request မဟုတ်ပါ", show_alert=True)
        return

    await query.answer("Cancelling...")
    event = user_cancel_events.get(user_id)
    if event:
        event.set()
        waiting_for_range.discard(user_id)
        try: await query.edit_message_text("⏳ Cancelling...")
        except: pass
    else:
        await query.answer("Cancel ရန် မရှိပါ", show_alert=True)

# ═══════════════════════════════════════════════════════════
#  Admin Commands
# ═══════════════════════════════════════════════════════════
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        await update.message.reply_text("❌ Admin only"); return

    today       = datetime.now().strftime("%Y-%m-%d")
    today_count = _stats.get("today", {}).get(today, 0)
    unique      = len(set(_stats.get("users", [])))
    active      = 2 - processing_semaphore._value

    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"📄 Total PDFs:    {_stats.get('total_pdfs', 0)}\n"
        f"📑 Total Pages:   {_stats.get('total_pages', 0)}\n"
        f"👤 Unique Users:  {unique}\n"
        f"📅 Today:         {today_count}\n"
        f"🔄 Active:        {active}/2",
        parse_mode="Markdown"
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        await update.message.reply_text("❌ Admin only"); return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return

    text    = " ".join(ctx.args)
    users   = list(set(_stats.get("users", [])))
    ok = fail = 0
    for u in users:
        try:
            await ctx.bot.send_message(u, f"📢 {text}")
            ok += 1
        except:
            fail += 1

    await update.message.reply_text(f"📢 Broadcast ပြီး\n✅ {ok} | ❌ {fail}")

# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════
def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set!")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.Document.PDF,          handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(cb_pages,  pattern="^pages_"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern="^cancel_"))

    print("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
