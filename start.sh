#!/bin/bash
echo "Installing Playwright Chromium..."
playwright install chromium --with-deps
echo "Starting bot..."
python bot.py
