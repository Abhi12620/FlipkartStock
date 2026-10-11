# Flipkart Multi-Product Stock Dashboard

Versatile dashboard to track **multiple Flipkart products** with individual pincodes.

### Features
- Beautiful web dashboard
- Add / Delete multiple product links
- Separate pincode for each product
- Automatic Telegram notification when stock arrives
- Check every **100 seconds**
- Works 24/7 on Render free tier + UptimeRobot

---

## 1. Telegram Setup

1. Telegram → **@BotFather** → `/newbot` → copy **Token**
2. Telegram → **@userinfobot** → start → copy **Chat ID**

---

## 2. Deploy on Render

1. Upload these files to a new GitHub repo:
   - `app.py`
   - `requirements.txt`
   - `README.md`

2. Go to [render.com](https://render.com) → **New Web Service**

3. Connect GitHub repo

4. Settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance**: Free

5. **Environment Variables** add karo:
   ```
   TELEGRAM_BOT_TOKEN = your_bot_token
   TELEGRAM_CHAT_ID   = your_chat_id
   CHECK_INTERVAL     = 100
   ```

6. Deploy

---

## 3. Keep Alive (UptimeRobot)

1. [uptimerobot.com](https://uptimerobot.com) → free account
2. Add Monitor → HTTP(s)
3. URL: `https://your-app.onrender.com/health`
4. Interval: **5 minutes**

---

## 4. How to Use

1. Open your Render URL in browser (e.g. `https://your-app.onrender.com`)
2. Dashboard khulega
3. Product URL + Pincode daalo → **Add Product**
4. Multiple products add kar sakte ho
5. Jaise hi kisi ka stock aayega → Telegram pe message aa jayega

---

## Notes

- Products list `products.json` mein save hoti hai (redeploy pe reset ho sakti hai)
- Flipkart page structure change ho sakta hai – detection fail ho toh bata dena
- Personal use only
