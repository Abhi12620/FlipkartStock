# Flipkart Stock Notifier Bot

Simple bot that checks Flipkart product availability for your pincode and sends Telegram notification when stock comes.

**Product pre-filled:** ASUS ExpertBook P3 (from your short link)

---

## 1. Telegram Bot Setup (2 minutes)

1. Open Telegram → search **@BotFather**
2. Send `/newbot` → give name & username
3. Copy the **token** (looks like `123456:ABC...`)
4. Search **@userinfobot** → start → copy your **Chat ID** (number)

---

## 2. Local Test (optional)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and put your TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID + PINCODE
python app.py
```

---

## 3. Deploy on Render (Free + 24/7)

1. Create a new GitHub repository and upload these files:
   - `app.py`
   - `requirements.txt`
   - `.env.example` (optional)

2. Go to [render.com](https://render.com) → **New** → **Web Service**

3. Connect your GitHub repo

4. Settings:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free

5. **Environment Variables** (add these):
   ```
   PRODUCT_URL = https://www.flipkart.com/asus-expertbook-p3-ai-pc-ir-cam-1-yr-adp-intel-core-ultra-5-225h-16-gb-512-gb-ssd-windows-11-home-p3606ccap-mb0062ws-p3606ccap-mb0431ws-thin-light-laptop/p/itm2cc2f9b61c169?pid=COMHM9URRSHWXPKY
   PINCODE = your_pincode_here
   TELEGRAM_BOT_TOKEN = your_bot_token
   TELEGRAM_CHAT_ID = your_chat_id
   CHECK_INTERVAL = 180
   ```

6. Click **Deploy**

---

## 4. Keep it Alive (UptimeRobot)

Render free tier sleeps after 15 min of no traffic.

1. Go to [uptimerobot.com](https://uptimerobot.com) → free account
2. **Add New Monitor**
   - Monitor Type: **HTTP(s)**
   - URL: `https://your-app-name.onrender.com/health`
   - Interval: **5 minutes**
3. Create Monitor

Done! Ab PC bandh hone pe bhi bot chalega.

---

## Notes

- Flipkart page structure change ho sakta hai. Agar kabhi detection fail ho toh bata dena.
- CHECK_INTERVAL 120-300 seconds recommended (bahut aggressive mat rakho).
- Personal use only.
