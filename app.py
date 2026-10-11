import os
import time
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ================== CONFIG ==================
PRODUCT_URL = os.getenv("PRODUCT_URL", "https://www.flipkart.com/asus-expertbook-p3-ai-pc-ir-cam-1-yr-adp-intel-core-ultra-5-225h-16-gb-512-gb-ssd-windows-11-home-p3606ccap-mb0062ws-p3606ccap-mb0431ws-thin-light-laptop/p/itm2cc2f9b61c169?pid=COMHM9URRSHWXPKY")
PINCODE = os.getenv("PINCODE", "110001")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))  # seconds (3 minutes)
# ============================================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram credentials missing — notification skip")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=12)
        if r.status_code == 200:
            print("✅ Telegram notification sent")
        else:
            print("❌ Telegram error:", r.text)
    except Exception as e:
        print("❌ Telegram exception:", e)


def check_stock():
    """
    Flipkart product page se stock status nikaalta hai.
    Returns: (in_stock: bool | None, title: str)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.flipkart.com/",
    }

    try:
        session = requests.Session()
        session.headers.update(headers)

        # Try setting pincode via cookie (best effort)
        session.cookies.set("pincode", PINCODE, domain=".flipkart.com")

        r = session.get(PRODUCT_URL, timeout=18, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        page_text = r.text.upper()

        # Title extract
        title = "ASUS ExpertBook P3"
        for selector in [
            ("span", {"class": "VU-ZEz"}),
            ("h1", {}),
            ("span", {"class": lambda c: c and "B_NuCI" in c}),
        ]:
            tag = soup.find(selector[0], selector[1])
            if tag and tag.get_text(strip=True):
                title = tag.get_text(strip=True)
                break

        # ===== Stock Detection =====
        # 1. Text based (most reliable)
        if "NOTIFY ME" in page_text or "NOTIFYME" in page_text:
            return False, title

        if any(x in page_text for x in ["ADD TO CART", "BUY NOW", "ADDTOCART", "BUYNOW"]):
            return True, title

        # 2. Button text
        for btn in soup.find_all("button"):
            txt = btn.get_text(strip=True).upper()
            if "NOTIFY" in txt:
                return False, title
            if "ADD TO CART" in txt or "BUY NOW" in txt:
                return True, title

        # 3. Disabled / sold out indicators
        if "CURRENTLY UNAVAILABLE" in page_text or "OUT OF STOCK" in page_text:
            return False, title

        # Default: treat as out of stock (safer)
        return False, title

    except Exception as e:
        print("❌ Check error:", e)
        return None, str(e)


def background_checker():
    print("🚀 Stock checker started...")
    print(f"📦 Product : {PRODUCT_URL[:80]}...")
    print(f"📍 Pincode : {PINCODE}")
    print(f"⏱  Interval: {CHECK_INTERVAL} seconds\n")

    last_status = None

    while True:
        try:
            in_stock, title = check_stock()
            now = time.strftime("%Y-%m-%d %H:%M:%S")

            if in_stock is True:
                print(f"[{now}] ✅ IN STOCK  → {title[:60]}")
                if last_status is not True:
                    msg = (
                        f"🔥 <b>STOCK AVAILABLE!</b>\n\n"
                        f"<b>{title}</b>\n\n"
                        f"🔗 <a href='{PRODUCT_URL}'>Open Product</a>\n"
                        f"📍 Pincode: {PINCODE}"
                    )
                    send_telegram(msg)
                last_status = True

            elif in_stock is False:
                print(f"[{now}] ❌ OUT OF STOCK → {title[:60]}")
                last_status = False
            else:
                print(f"[{now}] ⚠️  Could not determine stock")

        except Exception as e:
            print("Background loop error:", e)

        time.sleep(CHECK_INTERVAL)


@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status": "running",
        "product": PRODUCT_URL[:70] + "...",
        "pincode": PINCODE,
        "check_interval_sec": CHECK_INTERVAL
    }), 200


if __name__ == "__main__":
    # Start background checker
    t = threading.Thread(target=background_checker, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
