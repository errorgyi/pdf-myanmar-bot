import os
import asyncio
import tempfile
import re
import time
from pathlib import Path

import pdfplumber
import fitz
from deep_translator import GoogleTranslator
from playwright.async_api import async_playwright

from telegram import Update, Document
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

TOKEN = os.getenv("TELEGRAM_TOKEN", "8767341498:AAEFyEg5aHmAs4hCy1E60xsCfCZtQ3fFbfA")

# ── Myanmar font (bundled in repo) ──────────────────────────
FONT_PATH = Path(__file__).parent / "fonts" / "NotoSansMyanmar-Regular.ttf"

# ── Translation ──────────────────────────────────────────────
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


# ── Extract + Translate all pages ───────────────────────────
async def process_pdf(pdf_path: str, progress_cb=None):
    translations = {}
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            pn = i + 1
            # Update every 5 pages to avoid Telegram flood control
            if progress_cb and (pn == 1 or pn % 5 == 0 or pn == total):
                bar = "▓" * int(pn/total*20) + "░" * (20 - int(pn/total*20))
                await progress_cb(
                    f"🌐 ဘာသာပြန်နေပါတယ် — {pn}/{total} မျက်နှာ\n"
                    f"{bar} {int(pn/total*100)}%"
                )
            text = page.extract_text()
            if text and text.strip():
                translations[pn] = translate_text(text)
            else:
                translations[pn] = "(ဤစာမျက်နှာတွင် ဘာသာပြန်မရရှိပါ)"
    return translations, total



# ── Build HTML (side-by-side landscape) ─────────────────────
def build_html(page_num: int, total: int, img_b64: str, myan_text: str) -> str:
    safe = (myan_text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>"))
    return f"""<!DOCTYPE html>
<html lang="my">
<head><meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    width:1123px; height:794px;
    font-family:'Noto Sans Myanmar','Myanmar Text',sans-serif;
    background:#f0f4ff; display:flex; flex-direction:column; overflow:hidden;
  }}
  .header {{
    background:#1a1a2e; color:white; padding:5px 16px; font-size:11px;
    display:flex; justify-content:space-between; font-family:Arial,sans-serif; flex-shrink:0;
  }}
  .main {{ display:flex; flex:1; overflow:hidden; }}
  .left {{
    width:50%; background:white; display:flex; flex-direction:column;
    border-right:3px solid #3b82f6;
  }}
  .left-label {{
    background:#1e3a6e; color:white; font-family:Arial,sans-serif;
    font-size:10px; text-align:center; padding:4px; flex-shrink:0;
  }}
  .left-img {{
    flex:1; display:flex; justify-content:center; align-items:center;
    padding:8px; overflow:hidden;
  }}
  .left-img img {{ max-width:100%; max-height:100%; object-fit:contain; }}
  .right {{
    width:50%; background:#fff; display:flex; flex-direction:column; overflow:hidden;
  }}
  .right-label {{
    background:#dbeafe; color:#1e3a6e; font-size:12px;
    padding:5px 14px; border-bottom:2px solid #93c5fd; flex-shrink:0;
  }}
  .translation {{
    flex:1; padding:12px 16px; font-size:13px; line-height:2.0;
    color:#0d1b2a; overflow:hidden; word-break:break-word;
  }}
  .footer {{
    background:#dbeafe; font-family:Arial,sans-serif; font-size:8px;
    color:#6b7280; text-align:center; padding:3px; flex-shrink:0;
  }}
</style></head>
<body>
  <div class="header">
    <span>n8n Community Edition &nbsp;|&nbsp; Page {page_num} / {total}</span>
    <span>မြန်မာဘာသာပြန်</span>
  </div>
  <div class="main">
    <div class="left">
      <div class="left-label">Original — Page {page_num}</div>
      <div class="left-img"><img src="data:image/png;base64,{img_b64}"></div>
    </div>
    <div class="right">
      <div class="right-label">မြန်မာဘာသာပြန်ချက် — စာမျက်နှာ {page_num}</div>
      <div class="translation">{safe}</div>
    </div>
  </div>
  <div class="footer">Myanmar PDF Translator Bot &nbsp;|&nbsp; Myanmar Text Font</div>
</body></html>"""


# ── Render HTML → PNG via Playwright ────────────────────────
async def html_to_png_bytes(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1123, "height": 794})
        await page.set_content(html, wait_until="networkidle")
        png = await page.screenshot(clip={"x":0,"y":0,"width":1123,"height":794})
        await browser.close()
    return png


# ── Assemble PNG list → PDF ──────────────────────────────────
def pngs_to_pdf(png_list: list[bytes]) -> bytes:
    doc = fitz.open()
    for png_bytes in png_list:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_bytes)
            tmp = f.name
        img_doc = fitz.open(tmp)
        pdf_bytes = img_doc.convert_to_pdf()
        img_doc.close()
        img_pdf = fitz.open("pdf", pdf_bytes)
        doc.insert_pdf(img_pdf)
        os.unlink(tmp)
    out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc.save(out.name)
    doc.close()
    with open(out.name, "rb") as f:
        data = f.read()
    os.unlink(out.name)
    return data


# ── Full pipeline ────────────────────────────────────────────
async def make_bilingual_pdf(pdf_path: str, progress_cb=None) -> bytes:
    import base64

    pdf_doc = fitz.open(pdf_path)
    total = len(pdf_doc)

    if progress_cb:
        await progress_cb(f"📄 PDF ဖတ်ပြီး ဘာသာပြန်နေပါတယ် ({total} မျက်နှာ)...\n⏳ ခဏစောင့်ပါ")

    translations, _ = await process_pdf(pdf_path, progress_cb=progress_cb)

    png_pages = []
    for i in range(total):
        pn = i + 1
        # Update every 5 pages to avoid Telegram flood control
        if progress_cb and (pn == 1 or pn % 5 == 0 or pn == total):
            bar = "▓" * int(pn/total*20) + "░" * (20 - int(pn/total*20))
            await progress_cb(
                f"✅ ဘာသာပြန်ပြီးပြီ!\n"
                f"🎨 PDF ဆောက်နေပါတယ် — {pn}/{total} မျက်နှာ\n"
                f"{bar} {int(pn/total*100)}%"
            )
        page = pdf_doc[i]
        mat = fitz.Matrix(130/72, 130/72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode()

        myan = translations.get(pn, "(ဘာသာပြန်မရရှိ)")
        html = build_html(pn, total, img_b64, myan)
        png = await html_to_png_bytes(html)
        png_pages.append(png)

    pdf_doc.close()
    if progress_cb:
        await progress_cb("📦 PDF စုစည်းနေပါတယ်... နည်းနည်းပဲ ကျန်တော့တယ်!")
    return pngs_to_pdf(png_pages)


# ═══════════════════════════════════════════════════════════
#  Telegram Bot Handlers
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! 👋\n\n"
        "📄 PDF ဖိုင်တစ်ခု ပို့လိုက်ပါ\n"
        "➡️ မြန်မာဘာသာပြန်ပြီး PDF ပြန်ပေးပါမယ်\n\n"
        "⚠️ ဘာသာပြန်ရန် ကြာနိုင်ပါသည် (2-5 min)"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 အသုံးပြုနည်း:\n\n"
        "1️⃣ PDF ဖိုင် ဒီ bot ကို ပို့ပါ\n"
        "2️⃣ ဘာသာပြန်ချင်တဲ့ PDF ဖြစ်ရပါမယ် (English)\n"
        "3️⃣ ခဏစောင့်ပါ — ဘာသာပြန်ပြီး PDF ပြန်ပေးမယ်\n\n"
        "📌 Layout: ဘယ် = မူရင်း | ညာ = မြန်မာ\n"
        "⚠️ Max 20 pages (ကြာနိုင်)"
    )

async def handle_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document

    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ PDF ဖိုင်သာ လက်ခံပါသည်")
        return

    if doc.file_size > 20 * 1024 * 1024:  # 20MB limit
        await update.message.reply_text("❌ ဖိုင် 20MB ထက်ကြီးနေပါသည်")
        return

    msg = await update.message.reply_text("⏳ PDF ဒေါင်းလုဒ်လုပ်နေပါတယ်...")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name

    try:
        file = await ctx.bot.get_file(doc.file_id)
        await file.download_to_drive(tmp_path)

        async def progress(text):
            await msg.edit_text(text)

        pdf_data = await make_bilingual_pdf(tmp_path, progress_cb=progress)

        await msg.edit_text("📤 PDF ပေးပို့နေပါတယ်...")
        await update.message.reply_document(
            document=pdf_data,
            filename=f"myanmar_{doc.file_name}",
            caption="✅ မြန်မာဘာသာပြန် PDF ပြီးပါပြီ!\nဘယ် = မူရင်း | ညာ = မြန်မာ"
        )
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ Error ဖြစ်သွားပါတယ်: {str(e)[:200]}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN environment variable not set!")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    print("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
