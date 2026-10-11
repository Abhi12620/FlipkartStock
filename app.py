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
ALERT_REPEAT_SEC = int(os.getenv("ALERT_REPEAT_SEC", "0"))  # 0 = stock rehne tak har check pe alert
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


# ---------- Detection helpers ----------
LOC_OUT = ("NOT DELIVERABLE", "DOES NOT DELIVER", "NOT SERVICEABLE", "UNSERVICEABLE",
           "CANNOT BE DELIVERED", "NOT AVAILABLE FOR DELIVERY")
CTA_EXACT = {"ADD TO CART", "BUY NOW", "GO TO CART", "BUY WITH EMI"}
OUT_EXACT = {"NOTIFY ME", "SOLD OUT", "COMING SOON", "OUT OF STOCK"}
OUT_SUBSTR = ("CURRENTLY UNAVAILABLE", "CURRENTLY OUT OF STOCK", "OUT OF STOCK")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def decide(texts):
    """texts = short VISIBLE strings (upper-case). Returns (True/False/None, reason)."""
    for t in texts:
        if any(k in t for k in LOC_OUT):
            return False, f"not deliverable: '{t[:60]}'"
    for t in texts:
        if t in CTA_EXACT:
            return True, f"button: '{t}'"
    for t in texts:
        if t in OUT_EXACT or any(k in t for k in OUT_SUBSTR):
            return False, f"out signal: '{t[:60]}'"
    return None, "no clear signal"


def html_texts(soup):
    """Visible strings, ORIGINAL case (scripts/styles hata ke)."""
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()          # scripts ke andar ke 'notify me' etc. ignore
    out = []
    for s in soup.stripped_strings:
        s = s.strip()
        if 0 < len(s) <= 200:
            out.append(s)
    return out


def json_texts(o, out=None):
    if out is None:
        out = []
    if isinstance(o, dict):
        for v in o.values():
            json_texts(v, out)
    elif isinstance(o, list):
        for v in o:
            json_texts(v, out)
    elif isinstance(o, str):
        s = o.strip().upper()
        if 0 < len(s) <= 120:
            out.append(s)
    return out



OFFER_LABELS = ("BANK OFFER", "SPECIAL PRICE", "PARTNER OFFER", "COMBO OFFER", "NO COST EMI", "CASHBACK")
PRICE_RE = re.compile(r"^₹\s?[\d,]+(\.\d+)?$")
DISC_RE = re.compile(r"^\d{1,2}%\s*OFF$", re.I)


def ld_price(soup):
    """JSON-LD (schema.org) se price - sabse reliable, scripts hatane se pehle call karo."""
    for sc in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or "")
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            offers = d.get("offers") if isinstance(d, dict) else None
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price"):
                try:
                    return int(float(str(offers["price"]).replace(",", "")))
                except Exception:
                    pass
    return None


def extract_details(raw_texts, price_from_ld):
    """raw_texts = visible strings (original case). Price, MRP, discount, card offers."""
    price = mrp = disc = None
    prices = []
    for t in raw_texts[:400]:
        if PRICE_RE.match(t):
            prices.append(int(re.sub(r"[^\d]", "", t.split(".")[0])))
        elif disc is None and DISC_RE.match(t):
            disc = t.upper().replace(" ", "")
    if prices:
        price = price_from_ld or prices[0]
        bigger = [p for p in prices[1:4] if p > price]
        mrp = bigger[0] if bigger else None
    else:
        price = price_from_ld

    offers = []
    i = 0
    while i < len(raw_texts):
        t = raw_texts[i]
        tu = t.upper()
        line = None
        if tu in OFFER_LABELS and i + 1 < len(raw_texts):
            line = f"{t}: {raw_texts[i + 1]}"
            i += 1
        elif any(tu.startswith(l) for l in OFFER_LABELS) and len(t) > 15:
            line = t
        elif "CARD" in tu and ("₹" in t or "%" in t) and any(k in tu for k in ("OFF", "CASHBACK", "DISCOUNT", "EMI")):
            line = t
        if line:
            line = re.sub(r"\s*T&C\s*$", "", line).strip()[:160]
            if line not in offers:
                offers.append(line)
        i += 1
        if len(offers) >= 10:
            break
    return {"price": price, "mrp": mrp, "discount": disc, "offers": offers}


def check_single_product(url: str, pincode: str):
    """Returns (in_stock: bool|None, title: str, reason: str).
    None = pata nahi chala (blocked / no signal) -> OUT OF STOCK nahi maanenge."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.flipkart.com/",
    }
    try:
        s = requests.Session()
        s.headers.update(headers)

        # 1) warm-up: cookies lene ke liye
        r0 = s.get(url, timeout=15, allow_redirects=True)
        if r0.status_code in (403, 429, 529) or r0.status_code >= 500:
            return None, "Blocked", f"Flipkart ne block kiya (HTTP {r0.status_code})", {}

        # 2) pincode apply karo (Flipkart ka page-fetch API) -> location-aware result
        rome_decision, rome_reason = None, "rome: no response"
        try:
            path = re.sub(r"^https?://[^/]+", "", r0.url)
            payload = {
                "pageUri": path,
                "locationContext": {"pincode": str(pincode), "changed": True},
                "isReloadRequest": True,
                "pageContext": {"fetchSeoData": True, "pageNumber": 1},
            }
            rh = {**headers, "Accept": "*/*", "Content-Type": "application/json",
                  "X-user-agent": UA + " FKUA/website/42/website/Desktop",
                  "Origin": "https://www.flipkart.com"}
            rr = s.post("https://2.rome.api.flipkart.com/api/4/page/fetch",
                        headers=rh, json=payload, timeout=15)
            if rr.status_code == 200:
                rome_decision, rome_reason = decide(json_texts(rr.json()))
                rome_reason = "rome: " + rome_reason
            else:
                rome_reason = f"rome: HTTP {rr.status_code}"
        except Exception as e:
            rome_reason = f"rome error: {str(e)[:60]}"

        # 3) page HTML (title + fallback)
        r = s.get(url, timeout=18, allow_redirects=True)
        if r.status_code in (403, 429, 529) or r.status_code >= 500:
            return None, "Blocked", f"Flipkart ne block kiya (HTTP {r.status_code})", {}
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        title = "Product"
        for sel in (("span", {"class": "VU-ZEz"}), ("h1", {}),
                    ("span", {"class": lambda c: c and "B_NuCI" in str(c)})):
            tag = soup.find(sel[0], sel[1])
            if tag and tag.get_text(strip=True):
                title = tag.get_text(strip=True)[:80]
                break
        if title == "Product":
            m = re.search(r'"name"\s*:\s*"([^"]{10,120})"', html)
            if m:
                title = m.group(1)[:80]

        ldp = ld_price(soup)                      # scripts hatane se pehle
        raw = html_texts(soup)
        details = extract_details(raw, ldp)
        upper = [t.upper() for t in raw if len(t) <= 120]

        if rome_decision is not None:
            return rome_decision, title, rome_reason, details

        if len(upper) < 20:
            return None, title, f"page khali/captcha lag raha hai ({rome_reason})", details
        h_decision, h_reason = decide(upper)
        return h_decision, title, f"html (pincode apply nahi hua ho sakta): {h_reason} | {rome_reason}", details

    except Exception as e:
        print(f"Check error ({url[:40]}...):", e)
        return None, "Error fetching", f"error: {str(e)[:80]}", {}


def fmt_price(item):
    p = item.get("price")
    if not p:
        return "Price: -"
    s = f"₹{p:,}"
    if item.get("mrp"):
        s += f" (MRP ₹{item['mrp']:,}"
        s += f", {item['discount']})" if item.get("discount") else ")"
    return s


def fmt_offers(item):
    offs = (item.get("offers") or [])[:5]
    if not offs:
        return ""
    return "🏷 <b>Offers</b>\n" + "\n".join("• " + o.replace("<", "").replace(">", "") for o in offs) + "\n\n"


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

    in_stock, title, reason, details = check_single_product(target["url"], target["pincode"])
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    with lock:
        for item in products:
            if item["id"] == product_id:
                if title not in ("Error fetching", "Blocked"):
                    item["title"] = title
                item["reason"] = reason
                for k in ("price", "mrp", "discount"):
                    if details.get(k):
                        item[k] = details[k]
                if details.get("offers"):
                    item["offers"] = details["offers"]
                item["last_checked"] = now
                old_status = item.get("last_status")

                if in_stock is True:
                    item["last_status"] = "IN_STOCK"
                    print(f"[{now}] ✅ IN STOCK → {title[:50]} ({reason})", flush=True)
                    now_ts = time.time()
                    first = old_status != "IN_STOCK"
                    due = (now_ts - item.get("last_alert_ts", 0)) >= ALERT_REPEAT_SEC
                    if first or due:
                        head = "🔥 <b>STOCK AVAILABLE!</b>" if first else "🔁 <b>STILL IN STOCK!</b>"
                        msg = (
                            f"{head}\n\n"
                            f"<b>{title}</b>\n\n"
                            f"💰 {fmt_price(item)}\n"
                            f"{fmt_offers(item)}"
                            f"📍 Pincode: {item['pincode']}\n"
                            f"🔗 <a href='{item['url']}'>Open Product</a>"
                        )
                        if send_telegram(msg):
                            item["last_alert_ts"] = now_ts
                elif in_stock is False:
                    item["last_status"] = "OUT_OF_STOCK"
                    print(f"[{now}] ❌ OUT OF STOCK → {title[:50]} ({reason})")
                else:
                    item["last_status"] = "UNKNOWN"
                    print(f"[{now}] ❓ UNKNOWN → {title[:50]} ({reason})", flush=True)
                break
        save_products()


checker_state = {"thread": None, "started": None, "last_loop": None, "loops": 0}


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def background_checker():
    checker_state["started"] = _now()
    print(f"🚀 Multi-product checker started (interval: {CHECK_INTERVAL}s)", flush=True)
    while True:
        try:
            with lock:
                current_list = list(products)

            for p in current_list:
                # har check alag thread mein, 90s se zyada atke to aage badho
                t = threading.Thread(target=update_product_status, args=(p["id"],), daemon=True)
                t.start()
                t.join(90)
                if t.is_alive():
                    print(f"⏱ check timeout (90s): {p['id']}", flush=True)
                time.sleep(2)

            checker_state["loops"] += 1
            checker_state["last_loop"] = _now()
            print(f"🔁 loop #{checker_state['loops']} done | {len(current_list)} products | next in {CHECK_INTERVAL}s", flush=True)
        except Exception as e:
            print("Checker loop error:", e, flush=True)
        time.sleep(CHECK_INTERVAL)


def ensure_checker():
    """Thread mara ho ya start hi na hua ho to dobara chalu."""
    th = checker_state["thread"]
    if th is None or not th.is_alive():
        th = threading.Thread(target=background_checker, daemon=True)
        checker_state["thread"] = th
        th.start()


@app.before_request
def _watchdog():
    ensure_checker()   # UptimeRobot ke har ping pe bhi check hota hai


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
            ${p.price ? `<div style="margin:2px 0 4px;"><span style="color:#4ade80;font-weight:700;font-size:1.05rem;">₹${Number(p.price).toLocaleString('en-IN')}</span>
              ${p.mrp ? `<span style="color:#64748b;text-decoration:line-through;margin-left:8px;font-size:0.82rem;">₹${Number(p.mrp).toLocaleString('en-IN')}</span>` : ''}
              ${p.discount ? `<span style="color:#fbbf24;margin-left:8px;font-size:0.82rem;">${p.discount}</span>` : ''}</div>` : ''}
            ${(p.offers && p.offers.length) ? `<details style="margin:4px 0;font-size:0.78rem;color:#94a3b8;"><summary style="cursor:pointer;color:#38bdf8;">🏷 ${p.offers.length} offers</summary>
              <ul style="margin:4px 0 0 16px;padding:0;">${p.offers.map(o => `<li style="margin-bottom:3px;">${o}</li>`).join('')}</ul></details>` : ''}
            <div class="url"><a href="${p.url}" target="_blank">${p.url}</a></div>
          </td>
          <td>${p.pincode}</td>
          <td><span class="status ${statusClass}">${statusText}</span><div style="color:#64748b;font-size:0.72rem;margin-top:4px;max-width:220px;">${p.reason || ''}</div></td>
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
        "check_interval_sec": CHECK_INTERVAL,
        "checker_alive": bool(checker_state["thread"] and checker_state["thread"].is_alive()),
        "checker_started": checker_state["started"],
        "checker_last_loop": checker_state["last_loop"],
        "checker_loops": checker_state["loops"],
        "server_time_ist": _now()
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


@app.route("/api/debug/<pid>")
def debug_product(pid):
    with lock:
        t = next((p for p in products if p["id"] == pid), None)
    if not t:
        return jsonify({"error": "not found"}), 404
    ins, title, reason, details = check_single_product(t["url"], t["pincode"])
    return jsonify({"in_stock": ins, "title": title, "reason": reason, "details": details})


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
_started = False


def start_background():
    """gunicorn pe bhi chalega (pehle sirf __main__ mein chalta tha)."""
    global _started
    if _started:
        return
    _started = True
    load_products()
    ensure_checker()


start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
