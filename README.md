# 24-hr-data-checker

Telegram phone number checker that runs 24/7 on Render with GitHub-based persistence.

## Features
- Upload CSV/JSON/TXT files with phone numbers via web dashboard
- Checks each number against Telegram API
- Saves results to GitHub automatically
- Resumes from checkpoint if service restarts
- UptimeRobot-compatible `/health` endpoint

## Setup

### 1. Generate Session String (run locally)
```bash
pip install telethon python-dotenv
python generate_session.py
```
Enter your phone number → enter OTP from Telegram → copy the session string.

### 2. Create GitHub Token
Go to https://github.com/settings/tokens → Generate new token (classic) → select `repo` scope → copy token.

### 3. Deploy on Render
1. Create **New Web Service** on [render.com](https://render.com)
2. Connect this GitHub repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
5. Add environment variables:
   - `TELEGRAM_API_ID`
   - `TELEGRAM_API_HASH`
   - `TELEGRAM_SESSION_STRING` (from step 1)
   - `GITHUB_TOKEN` (from step 2)
   - `GITHUB_REPO` = `yeshaswi3060/24-hr-data-cecker-`

### 4. Set Up UptimeRobot
Add HTTP monitor for `https://your-app.onrender.com/health` every 5 minutes.

## Usage
Open the Render URL → upload a CSV file → click Start Checking → results auto-save to `telegram_results/` in this repo.
