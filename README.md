# Myanmar PDF Translator Bot 🤖

PDF ဖိုင်ကို မြန်မာဘာသာပြန်ပြီး ဘယ်-ညာ layout နဲ့ PDF ပြန်ပေးတဲ့ Telegram Bot

## Features
- 📄 PDF upload → မြန်မာဘာသာပြန် → PDF download
- 🖥️ Layout: ဘယ် = မူရင်း | ညာ = မြန်မာ (Landscape)
- 🔤 Myanmar Text font (proper shaping)

## Commands
- `/start` - Bot စတင်သုံးမည်
- `/help` - အသုံးပြုနည်း

## Deploy on Render.com
1. GitHub repo ဖွင့်ပြီး code upload
2. Render.com မှာ New Web Service → Connect GitHub
3. Environment Variable: `TELEGRAM_TOKEN` = your token
4. Build Command: `pip install -r requirements.txt && playwright install chromium --with-deps`
5. Start Command: `python bot.py`
