"""
server.py — Bridge TradingView -> EA MT5 + Telegram
Deploy len Railway. 3 file: server.py, requirements.txt, railway.json

Luong:
  TradingView --POST /webhook--> Railway --luu RAM + gui Telegram-->
  EA MT5 --WebRequest GET /pull moi 2s--> lay tin hieu --> vao lenh
"""

import os
import time
import threading
import requests as rq
from flask import Flask, request, jsonify

app = Flask(__name__)

# === CAU HINH TELEGRAM =============================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8987222484:AAH4bl-MhtP_DO5-tpMar_yfrvTSEQu4tGg")
CHAT_ID   = os.environ.get("CHAT_ID", "1877388272")
TG_URL    = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
# ===================================================================

signals = []
lock = threading.Lock()
DEFAULT_SYMBOL = "XAUUSD"


def next_id():
    return int(time.time() * 1000)


def send_telegram(text):
    """Gui thong bao Telegram (non-blocking, khong lam cham webhook)."""
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


@app.route("/webhook", methods=["POST"])
def webhook():
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

    # Gui Telegram
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


@app.route("/pull", methods=["GET"])
def pull():
    with lock:
        out = list(signals)
        signals.clear()
    return jsonify(ok=True, signals=out), 200


@app.route("/", methods=["GET"])
def health():
    with lock:
        pending = len(signals)
    return jsonify(status="running", pending=pending), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[server] Starting on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
