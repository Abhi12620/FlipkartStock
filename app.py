import os
import json
import time
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "100"))  # 100 seconds
PRODUCTS_FILE = "products.json"
# ============================================

# In-memory + file storage
products = []          # list of dicts: {id, url, pincode, title, last_status, last_checked}
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
            print("✅ Telegram sent")
        else:
            print("❌ Telegram error:", r.text[:200])
    except Exception as e:
        print("❌ Telegram exception:", e)


def check_single_product(url: str, pincode: str):
    """Returns (in_stock: bool|None, title: str)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.flipkart.com/",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        session.cookies.set("pincode", pincode, domain=".flipkart.com")

        r = session.get(url, timeout=18, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        page_text = r.text.upper()

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

        # Stock detection
        if "NOTIFY ME" in page_text or "NOTIFYME" in page_text:
            return False, title
        if any(x in page_text for x in ["ADD TO CART", "BUY NOW", "ADDTOCART", "BUYNOW"]):
            return True, title
        if "CURRENTLY UNAVAILABLE" in page_text or "OUT OF STOCK" in page_text:
            return False, title

        for btn in soup.find_all("button"):
            txt = btn.get_text(strip=True).upper()
            if "NOTIFY" in txt:
                return False, title
            if "ADD TO CART" in txt or "BUY NOW" in txt:
                return True, title

        return False, title
    except Exception as e:
        print(f"Check error ({url[:40]}...):", e)
        return None, "Error"


def background_checker():
    print("🚀 Multi-product checker started (interval: {}s)".format(CHECK_INTERVAL))
    while True:
        with lock:
            current_list = list(products)

        for p in current_list:
            try:
                in_stock, title = check_single_product(p["url"], p["pincode"])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                with lock:
                    for item in products:
                        if item["id"] == p["id"]:
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
                                        f"📍 Pincode: {p['pincode']}\n"
                                        f"🔗 <a href='{p['url']}'>Open Product</a>"
                                    )
                                    send_telegram(msg)
                            elif in_stock is False:
                                item["last_status"] = "OUT_OF_STOCK"
                                print(f"[{now}] ❌ OUT OF STOCK → {title[:50]}")
                            else:
                                item["last_status"] = "UNKNOWN"
                            break
                    save_products()

            except Exception as e:
                print("Item check error:", e)

            time.sleep(2)  # small gap between products

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
    .container { max-width: 900px; margin: 0 auto; }
    h1 { font-size: 1.8rem; margin-bottom: 8px; color: #38bdf8; }
    .sub { color: #94a3b8; margin-bottom: 24px; font-size: 0.95rem; }
    .card { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
    .form-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
    input { flex: 1; min-width: 200px; padding: 12px 14px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #e2e8f0; font-size: 0.95rem; }
    input:focus { outline: none; border-color: #38bdf8; }
    button { padding: 12px 20px; border-radius: 8px; border: none; cursor: pointer; font-weight: 600; font-size: 0.95rem; }
    .btn-add { background: #22c55e; color: #fff; }
    .btn-add:hover { background: #16a34a; }
    .btn-del { background: #ef4444; color: #fff; padding: 6px 12px; font-size: 0.85rem; }
    .btn-del:hover { background: #dc2626; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 12px 10px; border-bottom: 1px solid #334155; font-size: 0.9rem; }
    th { color: #94a3b8; font-weight: 500; }
    .status { padding: 4px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }
    .in-stock { background: #166534; color: #bbf7d0; }
    .out-stock { background: #7f1d1d; color: #fecaca; }
    .unknown { background: #334155; color: #94a3b8; }
    .url { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #38bdf8; }
    .empty { text-align: center; color: #64748b; padding: 40px 0; }
    .info { background: #1e3a5f; border: 1px solid #1e40af; color: #93c5fd; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="container">
    <h1>🛒 Flipkart Stock Dashboard</h1>
    <p class="sub">Multiple products • Per-product pincode • Auto Telegram alert • Check every {{ interval }}s</p>

    <div class="info">
      💡 Add product links below. Jab bhi koi product aapke pincode pe stock mein aayega, Telegram pe notification aa jayega.
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
          <th></th>
        </tr></thead><tbody>`;

      data.forEach(p => {
        let statusClass = 'unknown';
        let statusText = p.last_status || 'PENDING';
        if (statusText === 'IN_STOCK') { statusClass = 'in-stock'; statusText = 'IN STOCK'; }
        if (statusText === 'OUT_OF_STOCK') { statusClass = 'out-stock'; statusText = 'OUT OF STOCK'; }

        html += `<tr>
          <td>
            <div style="font-weight:500;margin-bottom:4px;">${p.title || 'Loading...'}</div>
            <div class="url"><a href="${p.url}" target="_blank" style="color:#38bdf8;">${p.url}</a></div>
          </td>
          <td>${p.pincode}</td>
          <td><span class="status ${statusClass}">${statusText}</span></td>
          <td style="color:#94a3b8;font-size:0.85rem;">${p.last_checked || '-'}</td>
          <td><button class="btn-del" onclick="deleteProduct('${p.id}')">Delete</button></td>
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

    loadProducts();
    setInterval(loadProducts, 15000);  // refresh every 15s
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

    if not url or "flipkart.com" not in url:
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

    return jsonify({"success": True, "id": new_id})


@app.route("/api/products/<pid>", methods=["DELETE"])
def delete_product(pid):
    global products
    with lock:
        products = [p for p in products if p["id"] != pid]
        save_products()
    return jsonify({"success": True})


# ================== START ==================
if __name__ == "__main__":
    load_products()
    t = threading.Thread(target=background_checker, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
