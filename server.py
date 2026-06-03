"""
Gold Scalper Auto Trade Server
================================
- Nhận webhook từ TradingView
- Parse alert → tạo pending order
- EA MT5 poll mỗi giây để lấy lệnh mới
- Telegram thông báo toàn bộ
"""

import os, json, uuid
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Bot
import uvicorn

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID  = os.getenv("ADMIN_ID", "")
GROUP_CHAT_ID  = os.getenv("GROUP_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "gold42")
PORT           = int(os.getenv("PORT", 8000))

app = FastAPI(title="Gold Scalper Auto Trade")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

# Queue lệnh chờ EA xử lý
# { order_id: { action, entry, sl, tp, source, score, ts, confirmed } }
PENDING_ORDERS: dict = {}

# ─────────────────────────────────────
# PARSE ALERT TỪ TRADINGVIEW
# ─────────────────────────────────────
def parse_alert(text: str) -> dict | None:
    """
    Parse alert string từ V42:
    "[M5] 🟢 BUY @ 4525.40 | SL 4520.40 | TP 4531.40 | ⭐⭐ 67pts [V18] | 14:35 (VN)"
    """
    try:
        # Bỏ qua alert quản lý lệnh (BREAKEVEN, CUT LOSS, TIMEOUT)
        skip_keywords = ["BREAKEVEN", "CUT LOSS", "TIMEOUT", "DAO CHIEU"]
        if any(k in text for k in skip_keywords):
            return None

        # Xác định source
        source = "H1-US" if "[H1-US]" in text else "M5"

        # Xác định action
        action = "BUY" if "BUY @" in text or "BUY @" in text else "SELL"

        # Parse giá — tìm số sau "BUY @" hoặc "SELL @"
        def extract_price(label: str) -> float:
            idx = text.find(label)
            if idx < 0: return 0.0
            idx += len(label)
            num = ""
            for c in text[idx:].lstrip():
                if c.isdigit() or c == ".": num += c
                elif num: break
            return float(num) if num else 0.0

        entry = extract_price("BUY @ ")  if action == "BUY"  else extract_price("SELL @ ")
        sl    = extract_price("SL ")
        tp    = extract_price("TP ")

        # Parse score
        score = 0
        for part in text.split("|"):
            if "pts" in part:
                for tok in part.split():
                    if tok.replace("pts","").isdigit():
                        score = int(tok.replace("pts",""))
                        break

        if entry <= 0 or sl <= 0 or tp <= 0:
            return None

        return {
            "action":  action,
            "entry":   entry,
            "sl":      sl,
            "tp":      tp,
            "source":  source,
            "score":   score,
            "raw":     text
        }
    except Exception as e:
        print(f"Parse error: {e} | text: {text}")
        return None

# ─────────────────────────────────────
# TELEGRAM HELPER
# ─────────────────────────────────────
async def notify(msg: str, chat_id: str = None):
    if not bot: return
    targets = [ADMIN_CHAT_ID]
    if chat_id and chat_id not in targets:
        targets.append(chat_id)
    for cid in targets:
        if cid:
            try:
                await bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Telegram error: {e}")

# ─────────────────────────────────────
# WEBHOOK ENDPOINT — TradingView gọi
# ─────────────────────────────────────
@app.post("/webhook/{secret}")
async def receive_alert(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    body = (await request.body()).decode("utf-8").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    order = parse_alert(body)
    if not order:
        # Alert quản lý lệnh — chỉ forward Telegram, không tạo order
        await notify(f"📢 Alert quan ly:\n`{body}`")
        return JSONResponse({"status": "forwarded", "tradeable": False})

    # Tạo order ID duy nhất
    order_id = str(uuid.uuid4())[:8].upper()
    PENDING_ORDERS[order_id] = {
        **order,
        "order_id":  order_id,
        "ts":        datetime.now().isoformat(),
        "confirmed": False
    }

    # Thông báo Telegram — lệnh đang chờ EA xử lý
    emoji  = "🟢" if order["action"] == "BUY" else "🔴"
    src    = order["source"]
    notify_msg = (
        f"{emoji} *[{src}] LENH MOI — Cho EA xu ly*\n"
        f"`{order['action']} @ {order['entry']:.2f}`\n"
        f"SL: `{order['sl']:.2f}` | TP: `{order['tp']:.2f}`\n"
        f"Score: `{order['score']}pts` | ID: `{order_id}`"
    )
    await notify(notify_msg)

    return JSONResponse({
        "status":   "queued",
        "order_id": order_id,
        "action":   order["action"],
        "entry":    order["entry"]
    })

# ─────────────────────────────────────
# EA POLLING — MT5 EA gọi để lấy lệnh
# ─────────────────────────────────────
@app.get("/mt5/pending")
async def get_pending(symbol: str = "XAUUSD"):
    # Lấy lệnh đầu tiên chưa confirmed
    for oid, order in PENDING_ORDERS.items():
        if not order["confirmed"]:
            return JSONResponse({
                "order_id": oid,
                "action":   order["action"],
                "entry":    order["entry"],
                "sl":       order["sl"],
                "tp":       order["tp"],
                "source":   order["source"],
                "score":    order["score"]
            })
    return JSONResponse({})  # Không có lệnh mới

# ─────────────────────────────────────
# EA CONFIRM — MT5 EA báo đã xử lý
# ─────────────────────────────────────
@app.post("/mt5/confirm")
async def confirm_order(request: Request):
    data    = await request.json()
    oid     = data.get("order_id", "")
    success = data.get("success", False)

    if oid not in PENDING_ORDERS:
        return JSONResponse({"status": "not_found"})

    order = PENDING_ORDERS[oid]
    order["confirmed"] = True

    emoji = "✅" if success else "❌"
    msg   = (
        f"{emoji} *EA {'DA DAT LENH' if success else 'DAT LENH THAT BAI'}*\n"
        f"ID: `{oid}` | {order['action']} @ `{order['entry']:.2f}`"
    )
    await notify(msg)

    # Xóa lệnh cũ nếu queue quá dài (giữ 50 lệnh gần nhất)
    if len(PENDING_ORDERS) > 50:
        oldest = sorted(PENDING_ORDERS.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[:10]:
            del PENDING_ORDERS[k]

    return JSONResponse({"status": "confirmed"})

# ─────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────
@app.get("/")
async def health():
    pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
    return {
        "status":          "running",
        "pending_orders":  pending,
        "total_received":  len(PENDING_ORDERS),
        "time":            datetime.now().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
