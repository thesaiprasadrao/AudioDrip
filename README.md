# Telegram Music Bot

Interactive YouTube → MP3 bot with search, selection, thumbnails, retries, and an audio cutter (/cut).

## Features
- Search top 3 results and pick with inline buttons
- Download and send MP3 (128kbps), dynamic filenames, thumbnails when possible
- Robust retries and fallbacks for sending
- /cut <start> <end> to trim the last downloaded audio segment

## Prerequisites
- Telegram bot token from @BotFather
- ffmpeg (auto-installed in Docker image; locally install via `brew install ffmpeg` on macOS)

## Configuration
Set the token via environment variable:
- BOT_TOKEN: Your Telegram bot token

Local development supports a `.env` file:
```
BOT_TOKEN=123456:ABC...
```

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=123456:ABC...
python bot.py
```

## Docker
Build and run:
```bash
# Build
docker build -t tg-bot .

# Run (replace token)
docker run -e BOT_TOKEN=123456:ABC... --name tg-bot --restart unless-stopped -d tg-bot
```

### Update
```bash
docker pull <your-registry>/tg-bot:latest
# or rebuild locally
```

## Deploy options
- Any VPS (DigitalOcean, Linode, Azure VM, EC2) with Docker
- Render or Railway using Dockerfile
- Fly.io or Koyeb (Docker)

Make sure outbound internet is allowed, and no inbound ports are needed (the bot uses long polling).

## Notes
- Bot uses environment BOT_TOKEN; do not hardcode secrets.
- If /cut is missing in menu, type it manually or restart chat; commands are set on startup.
- Cache directory is cleaned periodically.
