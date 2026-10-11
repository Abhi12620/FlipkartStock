import os
import re
import json
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string
from bs4 import BeautifulSoup
from dotenv import load_dotenv

IST = timezone(timedelta(hours=5, minutes=30))

load_dotenv()

app = Flask(__name__)

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "100"))  # 100 seconds
PRODUCTS_FILE = "products.json"
# ============================================

products = []
lock = threading.Lock()


def load_products():
    global products
    try:
        if os.path.exists(PRODUCTS_FILE):
            with open(PRODUCTS_FILE, "r") as f:
                products = json.load(f)
    except Exception:
        products = []


def save_products():
    try:
        with open(PRODUCTS_FILE, "w") as f:
            json.dump(products, f, indent=2)
    except Exception as e:
        print("Save error:", e)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=12)
        if r.status_code == 200:
            print("✅ Telegram sent")
            return True
        else:
            print("❌ Telegram error:", r.text[:200])
            return False
    except Exception as e:
        print("❌ Telegram exception:", e)
        return False


def check_single_product(url: str, pincode: str):
    """Returns (in_stock: bool|None, title: str)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.flipkart.com/",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        session = requests.Session()
        session.headers.update(headers)

        # Step 1: Try to set pincode via Flipkart location API (best effort)
        try:
            loc_headers = {
                **headers,
                "Content-Type": "application/json",
                "X-user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 FKUA/website/42/website/Desktop",
            }
            # First fetch product to get session cookie
            r0 = session.get(url, timeout=12, allow_redirects=True)
            # Try rome API to set location context
            rome_url = "https://1.rome.api.flipkart.com/api/4/page/fetch"
            payload = {
                "pageUri": url.replace("https://www.flipkart.com", ""),
                "locationContext": {"pincode": str(pincode)},
                "pageContext": {"pageNumber": 1, "fetchSeoData": True}
            }
            r_rome = session.post(rome_url, headers=loc_headers, json=payload, timeout=12)
            if r_rome.status_code == 200:
                j = r_rome.json()
                # Try to extract availability from API response
                raw = str(j).upper()
                title = "Product"
                # Try finding title in response
                try:
                    # common paths vary, just search text
                    if "PRODUCT" in raw or "TITLE" in raw:
                        pass
                except Exception:
                    pass

                if "NOTIFY ME" in raw or "NOTIFYME" in raw:
                    return False, title
                if "NOT DELIVERABLE AT YOUR LOCATION" in raw:
                    return False, title
                if any(x in raw for x in ["BUY NOW", "ADD TO CART", "ADDTOCART", "BUYNOW", "IN_STOCK", "INSTOCK"]):
                    # Try extract better title later
                    pass
                # Save SN cookie if present
                if "SESSION" in j and isinstance(j.get("SESSION"), dict):
                    sn = j["SESSION"].get("sn")
                    if sn:
                        session.cookies.set("SN", sn, domain=".flipkart.com")
        except Exception as e:
            print("Rome API skip:", e)

        # Step 2: Normal page fetch with cookies
        session.cookies.set("pincode", pincode, domain=".flipkart.com")
        r = session.get(url, timeout=18, allow_redirects=True)
        r.raise_for_status()
        html = r.text
        page_text = html.upper()
        soup = BeautifulSoup(html, "html.parser")

        # Title
        title = "Product"
        for sel in [
            ("span", {"class": "VU-ZEz"}),
            ("h1", {}),
            ("span", {"class": lambda c: c and "B_NuCI" in str(c)}),
        ]:
            tag = soup.find(sel[0], sel[1])
            if tag and tag.get_text(strip=True):
                title = tag.get_text(strip=True)[:80]
                break

        # Also try from JSON-LD or window data
        if title == "Product":
            import re
            m = re.search(r'"name"\s*:\s*"([^"]{10,120})"', html)
            if m:
                title = m.group(1)[:80]

        # ----- Detection -----
        # OUT signals
        if "NOTIFY ME" in page_text or "NOTIFYME" in page_text:
            return False, title
        if "CURRENTLY UNAVAILABLE" in page_text:
            return False, title
        if "NOT DELIVERABLE AT YOUR LOCATION" in page_text:
            return False, title
        if "DOES NOT DELIVER TO YOUR LOCATION" in page_text:
            return False, title

        # IN signals (broader)
        in_signals = [
            "BUY NOW", "BUYNOW", "ADD TO CART", "ADDTOCART",
            "BUY WITH EMI", "BUY WITH",
            '"AVAILABLE":TRUE', '"INSTOCK":TRUE',
            '"IN_STOCK"', "IN STOCK",
        ]
        for sig in in_signals:
            if sig in page_text:
                return True, title

        # Button / div text
        for el in soup.find_all(["button", "div", "span", "a"]):
            txt = (el.get_text(strip=True) or "").upper()
            if len(txt) > 30:
                continue
            if txt in ("NOTIFY ME", "NOTIFYME"):
                return False, title
            if any(x in txt for x in ("BUY NOW", "ADD TO CART", "BUY WITH EMI")):
                return True, title

        # Last resort: if price is clearly visible and no notify me → likely in stock
        if "₹" in html and "NOTIFY ME" not in page_text:
            # weak signal
            if "DELIVERY BY" in page_text or "DELIVERY IN" in page_text:
                return True, title

        return False, title
    except Exception as e:
        print(f"Check error ({url[:40]}...):", e)
        return None, "Error fetching"


def update_product_status(product_id: str):
    """Immediately check one product and update status + send notification if needed"""
    with lock:
        target = None
        for p in products:
            if p["id"] == product_id:
                target = p
                break
        if not target:
            return

    in_stock, title = check_single_product(target["url"], target["pincode"])
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    with lock:
        for item in products:
            if item["id"] == product_id:
                item["title"] = title
                item["last_checked"] = now
                old_status = item.get("last_status")

                if in_stock is True:
                    item["last_status"] = "IN_STOCK"
                    print(f"[{now}] ✅ IN STOCK → {title[:50]}")
                    if old_status != "IN_STOCK":
                        msg = (
                            f"🔥 <b>STOCK AVAILABLE!</b>\n\n"
                            f"<b>{title}</b>\n\n"
                            f"📍 Pincode: {item['pincode']}\n"
                            f"🔗 <a href='{item['url']}'>Open Product</a>"
                        )
                        send_telegram(msg)
                elif in_stock is False:
                    item["last_status"] = "OUT_OF_STOCK"
                    print(f"[{now}] ❌ OUT OF STOCK → {title[:50]}")
                else:
                    item["last_status"] = "UNKNOWN"
                break
        save_products()


def background_checker():
    print(f"🚀 Multi-product checker started (interval: {CHECK_INTERVAL}s)")
    while True:
        with lock:
            current_list = list(products)

        for p in current_list:
            try:
                update_product_status(p["id"])
            except Exception as e:
                print("Item check error:", e)
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)


# ================== DASHBOARD HTML ==================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Flipkart Stock Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 20px; }
    .container { max-width: 950px; margin: 0 auto; }
    h1 { font-size: 1.8rem; margin-bottom: 8px; color: #38bdf8; }
    .sub { color: #94a3b8; margin-bottom: 20px; font-size: 0.95rem; }
    .card { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
    .form-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
    input { flex: 1; min-width: 180px; padding: 12px 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #e2e8f0; font-size: 0.95rem; }
    input:focus { outline: none; border-color: #38bdf8; }
    button { padding: 12px 18px; border-radius: 8px; border: none; cursor: pointer; font-weight: 600; font-size: 0.9rem; }
    .btn-add { background: #22c55e; color: #fff; }
    .btn-add:hover { background: #16a34a; }
    .btn-del { background: #ef4444; color: #fff; padding: 6px 12px; font-size: 0.8rem; }
    .btn-del:hover { background: #dc2626; }
    .btn-test { background: #8b5cf6; color: #fff; }
    .btn-test:hover { background: #7c3aed; }
    .btn-refresh { background: #0ea5e9; color: #fff; padding: 6px 12px; font-size: 0.8rem; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 12px 8px; border-bottom: 1px solid #334155; font-size: 0.88rem; }
    th { color: #94a3b8; font-weight: 500; }
    .status { padding: 4px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
    .in-stock { background: #166534; color: #bbf7d0; }
    .out-stock { background: #7f1d1d; color: #fecaca; }
    .unknown { background: #334155; color: #94a3b8; }
    .pending { background: #854d0e; color: #fef08a; }
    .url { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .url a { color: #38bdf8; text-decoration: none; }
    .empty { text-align: center; color: #64748b; padding: 40px 0; }
    .info { background: #1e3a5f; border: 1px solid #1e40af; color: #93c5fd; padding: 12px 16px; border-radius: 8px; margin-bottom: 18px; font-size: 0.9rem; }
    .top-actions { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  </style>
</head>
<body>
  <div class="container">
    <h1>🛒 Flipkart Stock Dashboard</h1>
    <p class="sub">Multiple products • Per-product pincode • Auto Telegram alert • Check every {{ interval }}s</p>

    <div class="info">
      💡 Product add karte hi turant check hota hai. Stock aaye toh Telegram notification aayega.
    </div>

    <div class="top-actions">
      <button class="btn-test" onclick="testNotification()">🔔 Test Telegram Notification</button>
    </div>

    <div class="card">
      <h3 style="margin-bottom:14px;">➕ Add New Product</h3>
      <div class="form-row">
        <input type="text" id="url" placeholder="Flipkart product URL (https://www.flipkart.com/...)" />
        <input type="text" id="pincode" placeholder="Pincode (e.g. 110001)" maxlength="6" style="max-width:140px;" />
        <button class="btn-add" onclick="addProduct()">Add Product</button>
      </div>
    </div>

    <div class="card">
      <h3 style="margin-bottom:14px;">📦 Tracked Products (<span id="count">0</span>)</h3>
      <div id="list">
        <div class="empty">No products added yet</div>
      </div>
    </div>
  </div>

  <script>
    async function loadProducts() {
      const res = await fetch('/api/products');
      const data = await res.json();
      const list = document.getElementById('list');
      const count = document.getElementById('count');
      count.textContent = data.length;

      if (data.length === 0) {
        list.innerHTML = '<div class="empty">No products added yet</div>';
        return;
      }

      let html = `<table>
        <thead><tr>
          <th>Product</th>
          <th>Pincode</th>
          <th>Status</th>
          <th>Last Checked</th>
          <th>Actions</th>
        </tr></thead><tbody>`;

      data.forEach(p => {
        let statusClass = 'pending';
        let statusText = p.last_status || 'PENDING';
        if (statusText === 'IN_STOCK') { statusClass = 'in-stock'; statusText = 'IN STOCK'; }
        else if (statusText === 'OUT_OF_STOCK') { statusClass = 'out-stock'; statusText = 'OUT OF STOCK'; }
        else if (statusText === 'UNKNOWN') { statusClass = 'unknown'; statusText = 'UNKNOWN'; }
        else { statusClass = 'pending'; statusText = 'PENDING'; }

        html += `<tr>
          <td>
            <div style="font-weight:500;margin-bottom:4px;">${p.title || 'Checking...'}</div>
            <div class="url"><a href="${p.url}" target="_blank">${p.url}</a></div>
          </td>
          <td>${p.pincode}</td>
          <td><span class="status ${statusClass}">${statusText}</span></td>
          <td style="color:#94a3b8;font-size:0.82rem;">${p.last_checked || '-'}</td>
          <td>
            <button class="btn-refresh" onclick="refreshProduct('${p.id}')">Refresh</button>
            <button class="btn-del" onclick="deleteProduct('${p.id}')">Delete</button>
          </td>
        </tr>`;
      });

      html += '</tbody></table>';
      list.innerHTML = html;
    }

    async function addProduct() {
      const url = document.getElementById('url').value.trim();
      const pincode = document.getElementById('pincode').value.trim();
      if (!url || !pincode) { alert('URL aur Pincode dono daalo'); return; }
      if (pincode.length !== 6) { alert('Pincode 6 digit ka hona chahiye'); return; }

      const res = await fetch('/api/products', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, pincode })
      });
      if (res.ok) {
        document.getElementById('url').value = '';
        document.getElementById('pincode').value = '';
        loadProducts();
        // status update hone ke liye thoda wait
        setTimeout(loadProducts, 4000);
        setTimeout(loadProducts, 8000);
      } else {
        const err = await res.json();
        alert(err.error || 'Error adding product');
      }
    }

    async function deleteProduct(id) {
      if (!confirm('Delete this product?')) return;
      await fetch('/api/products/' + id, { method: 'DELETE' });
      loadProducts();
    }

    async function refreshProduct(id) {
      await fetch('/api/products/' + id + '/refresh', { method: 'POST' });
      setTimeout(loadProducts, 3000);
      setTimeout(loadProducts, 6000);
    }

    async function testNotification() {
      const res = await fetch('/api/test-telegram', { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        alert('✅ Test notification bhej diya gaya! Telegram check karo.');
      } else {
        alert('❌ Failed: ' + (data.error || 'Check TELEGRAM_BOT_TOKEN & CHAT_ID'));
      }
    }

    loadProducts();
    setInterval(loadProducts, 12000);
  </script>
</body>
</html>
"""


# ================== ROUTES ==================
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, interval=CHECK_INTERVAL)


@app.route("/health")
def health():
    return jsonify({
        "status": "running",
        "products_count": len(products),
        "check_interval_sec": CHECK_INTERVAL
    })


@app.route("/api/products", methods=["GET"])
def get_products():
    with lock:
        return jsonify(products)


@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    pincode = (data.get("pincode") or "").strip()

    if not url or "flipkart.com" not in url.lower():
        return jsonify({"error": "Valid Flipkart URL required"}), 400
    if not pincode.isdigit() or len(pincode) != 6:
        return jsonify({"error": "Valid 6-digit pincode required"}), 400

    new_id = str(int(time.time() * 1000))
    item = {
        "id": new_id,
        "url": url,
        "pincode": pincode,
        "title": "Checking...",
        "last_status": "PENDING",
        "last_checked": None
    }

    with lock:
        products.append(item)
        save_products()

    # Turant background mein check start karo
    threading.Thread(target=update_product_status, args=(new_id,), daemon=True).start()

    return jsonify({"success": True, "id": new_id})


@app.route("/api/products/<pid>", methods=["DELETE"])
def delete_product(pid):
    global products
    with lock:
        products = [p for p in products if p["id"] != pid]
        save_products()
    return jsonify({"success": True})


@app.route("/api/products/<pid>/refresh", methods=["POST"])
def refresh_product(pid):
    threading.Thread(target=update_product_status, args=(pid,), daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/test-telegram", methods=["POST"])
def test_telegram():
    ok = send_telegram(
        "🔔 <b>Test Notification</b>\n\n"
        "Flipkart Stock Dashboard sahi se kaam kar raha hai!\n"
        "Agar yeh message aa gaya toh Telegram setup perfect hai ✅"
    )
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Telegram send failed. Check TOKEN & CHAT_ID"}), 400


# ================== START ==================
if __name__ == "__main__":
    load_products()
    t = threading.Thread(target=background_checker, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
