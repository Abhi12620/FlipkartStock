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


def json_strings(o, out=None):
    """JSON ke sab strings (original case, document order)."""
    if out is None:
        out = []
    if isinstance(o, dict):
        for v in o.values():
            json_strings(v, out)
    elif isinstance(o, list):
        for v in o:
            json_strings(v, out)
    elif isinstance(o, str):
        s = o.strip()
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



OFFER_LABELS = ("BANK OFFER", "SPECIAL PRICE", "PARTNER OFFER", "COMBO OFFER")
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


NUM_RE = re.compile(r"^₹?\s?(\d[\d,]*)$")
AMT_RE = re.compile(r"^(₹\s?[\d,]+(\.\d+)?|\d{1,2}(\.\d+)?\s?%)\s*(off|cashback|instant discount|unlimited cashback)\b.*$", re.I)
CARD_WORDS = re.compile(r"CARD|BANK|UPI|EMI|WALLET|AXIS|SBI|HDFC|ICICI|KOTAK|AMEX|FEDERAL|IDFC|BOB|PNB|CITI|RBL|YES BANK|ONECARD|PAYTM|PHONEPE|CRED\b", re.I)
SKIP_TXT = {"APPLY", "VIEW ALL", "VIEW ALL OFFERS", "BEST VALUE FOR YOU", "BANK OFFERS", "T&C", "TERMS & CONDITIONS"}
DELIV_RE = re.compile(r"^(DELIVERY|GET IT)\s+(BY|IN|ON)\b")


SECTION_HEADS = {
    "BANK OFFERS": "Bank offers", "BANK OFFER": "Bank offers",
    "EMI OFFERS": "EMI offers", "INSTANT LOAN OFFERS": "Instant loan offers",
    "NO COST EMI OFFERS": "No cost EMI", "PARTNER OFFERS": "Partner offers",
    "CREDIT CARD OFFERS": "Credit card offers", "WALLET OFFERS": "Wallet offers",
}
OFFER_AMT_RE = re.compile(
    r"^(₹\s?[\d,]+(\.\d+)?|\d{1,2}(\.\d+)?\s?%)\s*(off\b|cashback\b|/\s?m(o|onth)?\b|per month\b)", re.I)
STRONG_CARD = re.compile(
    r"(CREDIT|DEBIT)\s+CARD|BANK|FINANCE|AXIS|SBI|HDFC|ICICI|KOTAK|AMEX|FEDERAL|IDFC|PNB|CITI|RBL|YES\b|ONECARD|BAJAJ|SLICE|UPI|WALLET|PAYTM|PHONEPE|CARDLESS|LAZYPAY|ZESTMONEY|SIMPL|PAY LATER|NO COST EMI", re.I)
JUNK_TXT = {"APPLY", "VIEW ALL", "VIEW ALL OFFERS", "T&C", "TERMS & CONDITIONS", "KNOW MORE"}


ENUM_WORDS = {"TEXT", "IMAGE", "ICON", "BUTTON", "LINK", "TRUE", "FALSE", "NULL", "NONE", "DEFAULT",
              "VALUE", "ACTION", "CONTENT", "TITLE", "SUBTITLE", "HEADER", "FOOTER", "RICH_TEXT"}
ENUM_RE = re.compile(r"^[A-Za-z]+(Details|Content|Widget|Component|Renderable|Value|Type|Data|Info|Text)$|^[A-Z]+_[A-Z_]+$")
CARD_NAME_RE = re.compile(r"((?:[A-Z][\w&.\-]*\s+){0,3}(?:Credit|Debit)\s+Cards?)")


def _is_junk(s):
    su = s.upper()
    if su in JUNK_TXT or su.startswith("T&C") or su.startswith("HTTP"):
        return True
    if su in ENUM_WORDS or ENUM_RE.match(s):                 # 'ContentDetails', 'TEXT'
        return True
    if " " not in s and (len(s) > 25 or "_" in s):          # IDs jaise 13.ATLAS_PRODUCT...
        return True
    return len(s) > 140


def _amount_ok(s):
    if not OFFER_AMT_RE.match(s):
        return False
    m = re.search(r"[\d,]+(\.\d+)?", s)
    return bool(m) and float(m.group(0).replace(",", "") or 0) > 0   # '₹0 off' hatao


def _rupees(s):
    return [int(x.replace(",", "")) for x in re.findall(r"₹\s?([\d,]+)", s)]


def _make_offer(sec, strings):
    amt = next(s for s in strings if _amount_ok(s))
    star = any(s.upper() == "BEST VALUE FOR YOU" for s in strings)

    # total discount: "₹3,074 cashback + ₹4,510 instant discount" -> 7,584
    total = None
    if re.search(r"cashback|off|discount", amt, re.I) and not re.search(r"/\s?m", amt, re.I):
        nums = _rupees(amt)
        if nums:
            total = sum(nums) if "+" in amt else nums[0]

    short = [s for s in strings if s is not amt and not _is_junk(s) and s.upper() != "BEST VALUE FOR YOU"
             and s.upper() not in SECTION_HEADS and not _amount_ok(s)]
    extra = next((s for s in short if "•" in s), "")
    card = next((s for s in short if "•" not in s and len(s) <= 40 and STRONG_CARD.search(s)), "")
    if not card:                                   # naam lambe text (T&C) ke andar ho sakta hai
        for s in strings:
            m = CARD_NAME_RE.search(s)
            if m:
                card = m.group(1).strip()
                break
    if not card and short:
        card = short[0]

    if total:
        body = f"₹{total:,} off"
        detail = "" if amt.lower().replace(" ", "") == body.lower().replace(" ", "") else f" ({amt})"
        text = f"{'⭐ ' if star else ''}{card or 'Card offer'}: {body}{detail}"
    else:
        text = f"{'⭐ ' if star else ''}{card or 'Offer'}: {amt}"
    if extra:
        text += f" | {extra}"
    o = {"sec": sec or "Offers", "text": text[:220]}
    if total:
        o["total"] = total
    return o


def card_offers_tree(root, debug=None):
    """Rome JSON ko STRUCTURE se padho: ek card = aisa sabse bada node jisme sirf ek amount ho.
    Flat string pairing se garbage aata tha."""
    cache = {}

    def strs(node):
        k = id(node)
        if k in cache:
            return cache[k]
        out = []
        kids = node.values() if isinstance(node, dict) else node
        for ch in kids:
            if isinstance(ch, str):
                t = ch.strip()
                if t:
                    out.append(t)
            elif isinstance(ch, (dict, list)):
                out.extend(strs(ch))
            if len(out) > 40:
                break
        cache[k] = out[:41]
        return cache[k]

    offers, state = [], {"sec": None}

    def scan(node):
        if isinstance(node, str):
            h = SECTION_HEADS.get(node.strip().upper())
            if h:
                state["sec"] = h
            return
        if not isinstance(node, (dict, list)):
            return
        ss = strs(node)
        if len(ss) <= 14:
            amts = [s for s in ss if _amount_ok(s)]
            if len(amts) == 1:
                rest = " ".join(s for s in ss if s is not amts[0])
                if STRONG_CARD.search(rest):
                    for s in ss:                      # card ke andar heading ho to section update
                        h = SECTION_HEADS.get(s.upper())
                        if h:
                            state["sec"] = h
                    o = _make_offer(state["sec"], ss)
                    if debug is not None and len(debug) < 12:
                        debug.append(ss)
                    if o["text"].lower() not in [x["text"].lower() for x in offers]:
                        offers.append(o)
                    return
        for ch in (node.values() if isinstance(node, dict) else node):
            scan(ch)

    scan(root)
    return offers


def card_offers(raw):
    """HTML ke visible strings se (fallback). Section headings bhi track karta hai."""
    offers, state, i = [], {"sec": None}, 0
    n = len(raw)
    while i < n:
        t = raw[i].strip()
        h = SECTION_HEADS.get(t.upper())
        if h:
            state["sec"] = h
        elif _amount_ok(t) and not t.upper().startswith("BUY"):
            grp, j = [t], i + 1
            while j < n and len(grp) < 5:
                s = raw[j].strip()
                if _amount_ok(s) or s.upper() in SECTION_HEADS or s.upper() in CTA_EXACT or s.upper() == "APPLY":
                    break
                grp.append(s)
                j += 1
            if STRONG_CARD.search(" ".join(grp[1:])):
                o = _make_offer(state["sec"], grp)
                if o["text"].lower() not in [x["text"].lower() for x in offers]:
                    offers.append(o)
            i = j - 1
        i += 1
    return offers


def special_lines(raw):
    """Exchange + Wow-deal lines."""
    out = []
    for i, t in enumerate(raw):
        tu = t.strip().upper()
        if tu == "EXCHANGE OFFER" or tu.startswith("EXCHANGE OFFER "):
            nxt = [x.strip() for x in raw[i + 1:i + 5] if x.strip().upper() not in JUNK_TXT and not _is_junk(x.strip())]
            joined = (t + " " + " ".join(nxt)).upper()
            if "NOT AVAILABLE" in joined:
                out.append({"sec": "Exchange", "text": "Not available at this pincode"})
            # exchange value tumhare purane device pe depend karti hai -> skip
        elif tu.startswith("BUY AT ₹"):
            nxt = raw[i + 1].strip() if i + 1 < len(raw) else ""
            out.append({"sec": "Deal", "text": t.strip() + (" | " + nxt if nxt.upper().startswith("EMI") else "")})
    return out


def extract_details(raw_texts, price_from_ld, rome_json=None, rome_raw=None):
    """Price, MRP, discount, offers. raw_texts = visible strings (original case)."""
    price = price_from_ld
    if not price:
        for t in raw_texts[:300]:
            if PRICE_RE.match(t):
                price = int(re.sub(r"[^\d]", "", t.split(".")[0]))
                break

    # MRP: selling price ke bilkul aas-paas wali badi sankhya (ads / variants nahi)
    mrp = None
    if price:
        for i, t in enumerate(raw_texts):
            m = NUM_RE.match(t.strip())
            if not (m and int(m.group(1).replace(",", "")) == price):
                continue
            best = None
            for j in range(max(0, i - 4), min(len(raw_texts), i + 5)):
                mj = NUM_RE.match(raw_texts[j].strip())
                if mj and j != i:
                    v = int(mj.group(1).replace(",", ""))
                    if price < v < price * 10 and (best is None or abs(j - i) < best[0]):
                        best = (abs(j - i), v)
            if best:
                mrp = best[1]
                break
    disc = f"{round((mrp - price) / mrp * 100)}%OFF" if (mrp and price) else None

    raw_cards = []
    offers = card_offers_tree(rome_json, raw_cards) if rome_json is not None else []
    if not offers:
        offers = card_offers(raw_texts)
    if price:
        for o in offers:
            if o.get("total") and o["sec"] == "Bank offers" and o["total"] < price:
                o["text"] += f"  → ₹{price - o['total']:,}"
    bank = [o for o in offers if o.get("total")]
    if bank:
        best = max(bank, key=lambda o: o["total"])
        offers.insert(0, {"sec": "Best card offer", "text": best["text"].replace("⭐ ", "")})
    seen = set()
    for o in special_lines(raw_texts) + special_lines(rome_raw or []):
        if o["sec"] not in seen:                 # exchange/deal ek hi baar
            seen.add(o["sec"])
            offers.append(o)
    return {"price": price, "mrp": mrp, "discount": disc, "offers": offers[:30], "_raw_cards": raw_cards}


def decide2(h_up, r_up):
    """Page (visible text) ko priority. Rome JSON ka 'NOT DELIVERABLE' template-string ho sakta hai."""
    def first(lst, pred):
        return next((t for t in lst if pred(t)), None)

    h_loc = first(h_up, lambda t: any(k in t for k in LOC_OUT))
    h_cta = first(h_up, lambda t: t in CTA_EXACT)
    h_out = first(h_up, lambda t: t in OUT_EXACT or any(k in t for k in OUT_SUBSTR))
    deliv = first(h_up + r_up, lambda t: bool(DELIV_RE.match(t)))
    r_loc = first(r_up, lambda t: any(k in t for k in LOC_OUT))
    note = " | ⚠ rome 'not deliverable' text mila (template ho sakta hai, ignore kiya)" if (r_loc and not h_loc) else ""

    if h_loc:
        return False, f"page: not deliverable '{h_loc[:50]}'"
    if h_cta:
        return True, f"page button '{h_cta}'" + note
    if h_out:
        return False, f"page: '{h_out[:50]}'"
    if deliv:
        return True, f"delivery mila: '{deliv[:40]}'" + note
    if r_loc:
        return False, "rome: not deliverable (page pe koi buy button nahi)"
    return None, "no clear signal"


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

        # 2) pincode apply karo (Flipkart ka page-fetch API) -> location-aware data
        rome_raw, rome_json, rome_note = [], None, "rome: no response"
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
                rome_json = rr.json()
                rome_raw = json_strings(rome_json)
                rome_note = "rome:200"
            else:
                rome_note = f"rome:HTTP {rr.status_code}"
        except Exception as e:
            rome_note = f"rome error: {str(e)[:50]}"

        # 3) page HTML
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
        details = extract_details(raw, ldp, rome_json, rome_raw)

        h_up = [t.upper() for t in raw if len(t) <= 120]
        r_up = [t.upper() for t in rome_raw if len(t) <= 120]
        if len(h_up) < 20 and not r_up:
            return None, title, f"page khali/captcha lag raha hai ({rome_note})", details

        decision, why = decide2(h_up, r_up)
        return decision, title, f"{why} | {rome_note}", details

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
    import html as _h
    offs = item.get("offers") or []
    if not offs:
        return ""
    groups = {}
    for o in offs:
        sec, tx = (("Offers", o) if isinstance(o, str) else (o.get("sec", "Offers"), o.get("text", "")))
        groups.setdefault(sec, []).append(tx)
    lines = ["🏷 <b>Offers</b>"]
    for sec, items in groups.items():
        lines.append(f"<b>{_h.escape(sec)}</b>")
        lines += ["• " + _h.escape(x) for x in items[:6]]
    return "\n".join(lines)[:2200] + "\n\n"


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
                elif item.get("offers") and isinstance(item["offers"][0], str):
                    item["offers"] = []          # purana (galat format) offers saaf
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


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
    function offersHtml(list) {
      if (!list || !list.length) return '';
      const g = {};
      list.forEach(o => {
        const sec = (typeof o === 'string') ? 'Offers' : (o.sec || 'Offers');
        const tx = (typeof o === 'string') ? o : o.text;
        (g[sec] = g[sec] || []).push(tx);
      });
      let h = '<details open style="margin:6px 0;font-size:0.78rem;color:#cbd5e1;"><summary style="cursor:pointer;color:#38bdf8;">🏷 ' + list.length + ' offers</summary>';
      for (const sec in g) {
        h += '<div style="margin:6px 0 2px;color:#fbbf24;font-weight:600;">' + esc(sec) + '</div><ul style="margin:0 0 0 16px;padding:0;">';
        g[sec].forEach(t => { h += '<li style="margin-bottom:3px;">' + esc(t) + '</li>'; });
        h += '</ul>';
      }
      return h + '</details>';
    }
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
            ${offersHtml(p.offers)}
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
