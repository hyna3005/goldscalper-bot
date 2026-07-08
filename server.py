"""
server.py v3 — Bridge TradingView -> EA MT5 + Telegram 2 chieu
Deploy len Railway. Dieu khien EA tu Telegram.
 
Lenh Telegram:
  /status  - Xem trang thai
  /close   - Dong tat ca lenh dang mo
  /cancel  - Huy tat ca pending
  /stop    - Tam dung (khong vao lenh moi)
  /start   - Chay lai
  /help    - Danh sach lenh
"""
 
import os
import time
import threading
import requests as rq
from flask import Flask, request, jsonify
 
app = Flask(__name__)
 
# === CAU HINH ======================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8987222484:AAH4bl-MhtP_DO5-tpMar_yfrvTSEQu4tGg")
CHAT_ID   = os.environ.get("CHAT_ID", "1877388272")
TG_URL    = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
DEFAULT_SYMBOL = "XAUUSD"
# ===================================================================
 
signals = []
commands = []       # lenh tu Telegram cho EA: "CLOSE_ALL", "CANCEL_ALL", "STOP", "START"
paused = False      # True = tam dung, khong nhan tin hieu moi
lock = threading.Lock()
 
 
def next_id():
    return int(time.time() * 1000)
 
 
def send_telegram(text):
    def _send():
        try:
            rq.post(TG_URL, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
        except Exception as e:
            print(f"[tg] loi gui: {e}", flush=True)
    threading.Thread(target=_send, daemon=True).start()
 
 
def parse_tv_text(text):
    t = text.upper()
    side = None
    if "BUY" in t:
        side = "BUY"
    elif "SELL" in t:
        side = "SELL"
    if side is None:
        return None
    def grab(key):
        i = t.find(key)
        if i < 0:
            return None
        j = i + len(key)
        num = ""
        started = False
        while j < len(t):
            c = t[j]
            if c.isdigit() or c == '.' or (c == '-' and not started):
                num += c
                started = True
            elif started:
                break
            j += 1
        try:
            return float(num)
        except ValueError:
            return None
    et = grab("ET")
    sl = grab("SL")
    tp = grab("TP")
    if None in (et, sl, tp):
        return None
    return side, et, sl, tp
 
 
# === WEBHOOK TU TRADINGVIEW ========================================
@app.route("/webhook", methods=["POST"])
def webhook():
    global paused
    if paused:
        send_telegram("\u26a0\ufe0f <b>TAM DUNG</b> — tin hieu bi bo qua. /start de chay lai.")
        return jsonify(ok=False, reason="paused"), 200
 
    data = request.get_json(silent=True)
    side, et, sl, tp, symbol = None, None, None, None, DEFAULT_SYMBOL
 
    if data and isinstance(data, dict) and all(k in data for k in ("side", "et", "sl", "tp")):
        side = str(data["side"]).upper()
        et = float(data["et"])
        sl = float(data["sl"])
        tp = float(data["tp"])
        symbol = data.get("symbol", DEFAULT_SYMBOL)
    else:
        raw = request.get_data(as_text=True)
        parsed = parse_tv_text(raw)
        if parsed:
            side, et, sl, tp = parsed
 
    if not side or et is None or sl is None or tp is None:
        return jsonify(ok=False, reason="cannot parse"), 400
 
    sig_id = next_id()
    line = f"{sig_id};{side};{et};{sl};{tp};{symbol}"
    with lock:
        signals.append(line)
 
    print(f"[webhook] {line}", flush=True)
 
    emoji = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    sl_dist = abs(et - sl)
    tp_dist = abs(tp - et)
    rr = round(tp_dist / sl_dist, 1) if sl_dist > 0 else 0
    msg = (
        f"{emoji} <b>{side} {symbol}</b>\n"
        f"ET: {et}  |  SL: {sl}  |  TP: {tp}\n"
        f"SL: {sl_dist:.1f}  |  TP: {tp_dist:.1f}  |  RR: 1:{rr}\n"
        f"<i>Happy Scalp M5</i>"
    )
    send_telegram(msg)
    return jsonify(ok=True, line=line), 200
 
 
# === EA POLL =======================================================
@app.route("/pull", methods=["GET"])
def pull():
    global paused
    with lock:
        out_signals = list(signals)
        signals.clear()
        out_commands = list(commands)
        commands.clear()
    return jsonify(ok=True, signals=out_signals, commands=out_commands, paused=paused), 200
 
 
# === TELEGRAM BOT ==================================================
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    global paused
    data = request.get_json(silent=True)
    if not data:
        return "ok", 200
 
    msg = data.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip().lower()
 
    # Chi nhan lenh tu dung chat_id
    if chat_id != CHAT_ID:
        return "ok", 200
 
    if text == "/status":
        with lock:
            ps = len(signals)
            pc = len(commands)
        status = "\u23f8 TAM DUNG" if paused else "\u25b6\ufe0f DANG CHAY"
        send_telegram(
            f"\U0001f4ca <b>Trang thai</b>\n"
            f"Server: {status}\n"
            f"Tin hieu cho: {ps}\n"
            f"Lenh cho: {pc}"
        )
 
    elif text == "/close":
        with lock:
            commands.append("CLOSE_ALL")
        send_telegram("\U0001f534 <b>CLOSE ALL</b> — EA se dong tat ca lenh.")
 
    elif text == "/cancel":
        with lock:
            commands.append("CANCEL_ALL")
        send_telegram("\u274c <b>CANCEL ALL</b> — EA se huy tat ca pending.")
 
    elif text == "/stop":
        paused = True
        with lock:
            commands.append("STOP")
        send_telegram("\u23f8 <b>TAM DUNG</b> — EA ngung vao lenh moi. /start de chay lai.")
 
    elif text == "/start":
        paused = False
        with lock:
            commands.append("START")
        send_telegram("\u25b6\ufe0f <b>DA CHAY LAI</b> — EA tiep tuc nhan tin hieu.")
 
    elif text == "/help" or text == "/start" and not paused:
        send_telegram(
            "\U0001f916 <b>Lenh dieu khien</b>\n\n"
            "/status — Xem trang thai\n"
            "/close — Dong tat ca lenh dang mo\n"
            "/cancel — Huy tat ca pending\n"
            "/stop — Tam dung (khong vao lenh)\n"
            "/start — Chay lai\n"
            "/help — Hien thi lenh nay"
        )
 
    return "ok", 200
 
 
# === HEALTH ========================================================
@app.route("/", methods=["GET"])
def health():
    with lock:
        pending = len(signals)
    return jsonify(status="paused" if paused else "running", pending=pending), 200
 
 
# === SETUP TELEGRAM WEBHOOK KHI KHOI DONG =========================
def setup_telegram_webhook():
    """Tu dong dat webhook Telegram khi server khoi dong."""
    import time as _t
    _t.sleep(2)  # doi server san sang
    # Lay URL Railway tu bien moi truong hoac mac dinh
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not railway_url:
        railway_url = os.environ.get("RAILWAY_STATIC_URL", "")
    if not railway_url:
        railway_url = "goldscalper-bot-production.up.railway.app"
    
    url = f"https://{railway_url}/telegram"
    try:
        r = rq.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": url},
            timeout=10
        )
        print(f"[tg] setWebhook -> {url}: {r.json()}", flush=True)
    except Exception as e:
        print(f"[tg] loi setWebhook: {e}", flush=True)
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=setup_telegram_webhook, daemon=True).start()
    print(f"[server] Starting v3 on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
else:
    # Khi chay qua gunicorn
    threading.Thread(target=setup_telegram_webhook, daemon=True).start()
