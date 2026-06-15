FROM python:3.12-slim

# System deps: tesseract for OCR
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium
RUN playwright install chromium --with-deps

# App code
COPY . .

CMD ["python", "bot.py"]
