"""
server.py — Bridge TradingView -> EA MT5
Deploy len Railway, nhan webhook tu TradingView, luu tin hieu.
Script poll_railway.py tren may chay MT5 goi GET /pull de lay tin hieu moi.

Luong:
  TradingView --POST /webhook--> Railway (server.py) --luu vao RAM-->
  May ban: poll_railway.py --GET /pull--> lay tin hieu --> ghi file --> EA doc
"""

import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# Luu tin hieu trong RAM (list cac dong CSV)
# Railway free restart dinh ky -> mat tin hieu chua pull, nhung voi scalp M5
# chi can pull moi 2s nen hau nhu khong mat.
signals = []
lock = threading.Lock()

DEFAULT_SYMBOL = "XAUUSD"


def next_id():
    """ID tang dan theo thoi gian (millisecond UTC)."""
    return int(time.time() * 1000)


def parse_tv_text(text):
    """
    Parse message tho tu Pine alert, dang:
        BUY | ET 3421.5 | SL 3419.2 | TP 3424.9
    hoac co emoji dau dong.
    Tra ve (side, et, sl, tp) hoac None.
    """
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
    """TradingView ban POST vao day."""
    data = request.get_json(silent=True)
    side, et, sl, tp, symbol = None, None, None, None, DEFAULT_SYMBOL

    # Thu parse JSON truoc
    if data and isinstance(data, dict) and all(k in data for k in ("side", "et", "sl", "tp")):
        side = str(data["side"]).upper()
        et = float(data["et"])
        sl = float(data["sl"])
        tp = float(data["tp"])
        symbol = data.get("symbol", DEFAULT_SYMBOL)
    else:
        # Text tho
        raw = request.get_data(as_text=True)
        parsed = parse_tv_text(raw)
        if parsed:
            side, et, sl, tp = parsed

    if not side or et is None or sl is None or tp is None:
        return jsonify(ok=False, reason="cannot parse"), 400

    line = f"{next_id()};{side};{et};{sl};{tp};{symbol}"
    with lock:
        signals.append(line)

    print(f"[webhook] {line}", flush=True)
    return jsonify(ok=True, line=line), 200


@app.route("/pull", methods=["GET"])
def pull():
    """Script poll tren may goi GET /pull de lay tin hieu chua xu ly."""
    with lock:
        out = list(signals)
        signals.clear()
    return jsonify(ok=True, signals=out), 200


@app.route("/", methods=["GET"])
def health():
    """Health check cho Railway."""
    with lock:
        pending = len(signals)
    return jsonify(status="running", pending=pending), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[server] Starting on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
