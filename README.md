# TeraBox Downloader Bot (Telegram)

Fast, secure, multi-user TeraBox → Telegram downloader.

## Features
- Direct-link extraction (via `TeraboxDL`)
- Download + auto-clean temp files
- Pretty inline UI: /menu, /help, /limits, /privacy
- Size & rate limits; queue for stability
- aiogram v3 + httpx (async & fast)

## Setup
```bash
git clone <your-repo>
cd terabox-telegram-bot
cp .env.example .env
# Fill BOT_TOKEN and TERABOX_COOKIE (lang=...; ndus=...;)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.bot
