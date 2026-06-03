r · PY
"""
Gold Scalper Server v3.0
========================
- /telegram-webhook  : nhan tin nhan tu Telegram bot
- /webhook/{secret}  : nhan alert tu TradingView
- /mt5/pending       : EA poll lenh moi
- /mt5/confirm       : EA xac nhan da dat lenh
- /mt5/command       : EA poll lenh dieu khien
- Keep-alive: tu ping moi 4 phut de khong sleep
"""
 
import os, json, uuid, asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Bot
from telegram.error import TelegramError
import uvicorn
 
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))
GROUP_CHAT_ID  = os.getenv("GROUP_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "gold42")
APP_URL        = os.getenv("APP_URL", "")
PORT           = int(os.getenv("PORT", 8000))
 
app = FastAPI(title="Gold Scalper v3")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
 
# ── Keep-alive ───────────────────────────────────────────────
async def keep_alive():
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            if APP_URL:
                async with aiohttp.ClientSession() as s:
                    await s.get(f"{APP_URL}/ping",
                                timeout=aiohttp.ClientTimeout(total=10))
                    print(f"Keep-alive OK | {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"Keep-alive err: {e}")
        await asyncio.sleep(240)
 
# ── Database member ──────────────────────────────────────────
DB_FILE = "members.json"
 
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {"members": {}}
 
def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
 
def get_active_members():
    db = load_db()
    now = datetime.now()
    result = []
    for uid, info in db["members"].items():
        expire = datetime.fromisoformat(info["expire_date"])
        if expire > now:
            result.append({"user_id": int(uid), "expire_date": expire})
    return result
 
def add_member(user_id, username, days=30):
    db = load_db()
    now = datetime.now()
    if str(user_id) in db["members"]:
        cur = datetime.fromisoformat(db["members"][str(user_id)]["expire_date"])
        base = max(cur, now)
    else:
        base = now
    expire = base + timedelta(days=days)
    db["members"][str(user_id)] = {
        "username": username,
        "added_date": now.isoformat(),
        "expire_date": expire.isoformat()
    }
    save_db(db)
    return expire
 
def remove_member(user_id):
    db = load_db()
    db["members"].pop(str(user_id), None)
    save_db(db)
 
# ── State ────────────────────────────────────────────────────
PENDING_ORDERS: dict = {}
RECENT_ALERTS:  dict = {}
EA_COMMAND            = {"cmd": ""}
 
# ── Telegram helper ──────────────────────────────────────────
async def tg(chat_id, text):
    if not bot or not chat_id:
        return
    try:
        await bot.send_message(
            chat_id=int(chat_id),
            text=str(text),
            parse_mode="Markdown"
        )
    except TelegramError as e:
        print(f"TG err: {e}")
 
# ── Parse alert V42 ──────────────────────────────────────────
def parse_order(raw):
    skip = ["BREAKEVEN", "CUT LOSS", "TIMEOUT"]
    if any(k in raw for k in skip):
        return None
    try:
        action = None
        if "BUY @" in raw:
            action = "BUY"
        elif "SELL @" in raw:
            action = "SELL"
        if not action:
            return None
 
        source = "H1-US" if "[H1-US]" in raw else "M5"
 
        def get_price(label):
            idx = raw.find(label)
            if idx < 0:
                return 0.0
            num = ""
            for c in raw[idx + len(label):].lstrip():
                if c.isdigit() or c == ".":
                    num += c
                elif num:
                    break
            return float(num) if num else 0.0
 
        entry = get_price("BUY @ ") if action == "BUY" else get_price("SELL @ ")
        sl    = get_price("SL ")
        tp    = get_price("TP ")
 
        score = 0
        for part in raw.split("|"):
            if "pts" in part:
                for tok in part.split():
                    t = tok.replace("pts", "")
                    if t.isdigit():
                        score = int(t)
                        break
 
        if entry <= 0 or sl <= 0 or tp <= 0:
            return None
 
        return {
            "action": action,
            "entry":  entry,
            "sl":     sl,
            "tp":     tp,
            "source": source,
            "score":  score
        }
    except Exception as e:
        print(f"Parse err: {e}")
        return None
 
def format_alert(raw):
    if "BREAKEVEN" in raw:
        return f"BREAKEVEN\n`{raw}`\n_Keo SL ve hoa von ngay_"
    if "CUT LOSS" in raw:
        return f"CAT LENH\n`{raw}`\n_Dong lenh ngay_"
    if "TIMEOUT" in raw:
        return f"TIMEOUT\n`{raw}`\n_Xem xet dong lenh_"
    if "DAO CHIEU" in raw:
        return f"DAO CHIEU\n`{raw}`"
    if "BUY @" in raw:
        return f"BUY\n`{raw}`\n_Dat lenh BUY LIMIT theo gia E_"
    if "SELL @" in raw:
        return f"SELL\n`{raw}`\n_Dat lenh SELL LIMIT theo gia E_"
    return f"`{raw}`"
 
# ── Telegram webhook ─────────────────────────────────────────
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" not in data:
        return {"ok": True}
 
    msg      = data["message"]
    chat_id  = msg["chat"]["id"]
    user_id  = msg["from"]["id"]
    text     = msg.get("text", "").strip()
    is_admin = (user_id == ADMIN_ID)
 
    if text == "/start":
        if is_admin:
            reply = (
                "*Gold Scalper Bot v3 - Admin*\n\n"
                "*Quan ly member:*\n"
                "`/add <id> <ngay>` - Them member\n"
                "`/remove <id>` - Xoa member\n"
                "`/list` - Danh sach member\n"
                "`/stats` - Thong ke\n\n"
                "*Dieu khien EA:*\n"
                "`/ea_stop` - Tam dung EA\n"
                "`/ea_start` - Bat lai EA\n"
                "`/ea_close` - Dong tat ca lenh\n"
                "`/ea_status` - Trang thai EA\n\n"
                "`/test` - Test alert\n"
                "`/myid` - Xem ID cua ban"
            )
        else:
            reply = (
                "*Gold Scalper Alert Bot*\n\n"
                "Lien he admin de dang ky.\n"
                "Gõ /myid de lay ID cua ban."
            )
        await tg(chat_id, reply)
 
    elif text == "/myid":
        await tg(chat_id, f"ID cua ban: `{user_id}`")
 
    elif text.startswith("/add") and is_admin:
        parts = text.split()
        if len(parts) == 3:
            try:
                uid  = int(parts[1])
                days = int(parts[2])
                exp  = add_member(uid, "", days)
                await tg(chat_id,
                    f"Da them `{uid}` - Han: `{exp.strftime('%d/%m/%Y')}`")
            except Exception:
                await tg(chat_id, "Sai cu phap: `/add <id> <ngay>`")
        else:
            await tg(chat_id, "Sai cu phap: `/add <id> <ngay>`")
 
    elif text.startswith("/remove") and is_admin:
        parts = text.split()
        if len(parts) == 2:
            try:
                remove_member(int(parts[1]))
                await tg(chat_id, f"Da xoa `{parts[1]}`")
            except Exception:
                await tg(chat_id, "Sai cu phap: `/remove <id>`")
 
    elif text == "/list" and is_admin:
        members = get_active_members()
        if members:
            lines = [f"*{len(members)} member active:*\n"]
            for m in sorted(members, key=lambda x: x["expire_date"]):
                d = (m["expire_date"] - datetime.now()).days
                e = "🔴" if d <= 3 else "🟡" if d <= 7 else "🟢"
                lines.append(f"{e} `{m['user_id']}` - Con {d} ngay")
            await tg(chat_id, "\n".join(lines))
        else:
            await tg(chat_id, "Chua co member nao.")
 
    elif text == "/stats" and is_admin:
        db      = load_db()
        members = get_active_members()
        total   = len(db["members"])
        active  = len(members)
        soon    = sum(
            1 for m in members
            if (m["expire_date"] - datetime.now()).days <= 7
        )
        pending = sum(
            1 for o in PENDING_ORDERS.values()
            if not o["confirmed"]
        )
        await tg(chat_id,
            f"*Thong ke*\n"
            f"Member: `{active}` active / `{total}` tong\n"
            f"Sap het han: `{soon}`\n"
            f"Lenh cho EA: `{pending}`")
 
    elif text == "/test" and is_admin:
        test = "[M5] BUY @ 4525.40 | SL 4520.40 | TP 4531.40 | 67pts [V18] | 14:35 (VN)"
        await tg(chat_id, format_alert(test))
 
    elif text == "/ea_stop" and is_admin:
        EA_COMMAND["cmd"] = "stop"
        await tg(chat_id, "Lenh STOP da gui den EA")
 
    elif text == "/ea_start" and is_admin:
        EA_COMMAND["cmd"] = "start"
        await tg(chat_id, "Lenh START da gui den EA")
 
    elif text == "/ea_close" and is_admin:
        EA_COMMAND["cmd"] = "close"
        await tg(chat_id, "Lenh CLOSE da gui den EA")
 
    elif text == "/ea_status" and is_admin:
        pending = sum(
            1 for o in PENDING_ORDERS.values()
            if not o["confirmed"]
        )
        cmd = EA_COMMAND["cmd"] if EA_COMMAND["cmd"] else "running"
        await tg(chat_id,
            f"*EA Status*\n"
            f"Trang thai: `{cmd.upper()}`\n"
            f"Lenh cho xu ly: `{pending}`\n"
            f"Tong lenh nhan: `{len(PENDING_ORDERS)}`")
 
    return {"ok": True}
 
# ── TradingView webhook ──────────────────────────────────────
@app.post("/webhook/{secret}")
async def receive_alert(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
 
    raw = (await request.body()).decode("utf-8").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty")
 
    print(f"Alert: {raw[:100]}")
 
    order = parse_order(raw)
 
    # Dedup: bo qua neu cung lenh trong 60 giay
    if order:
        key = f"{order['action']}_{order['entry']}"
        now = datetime.now()
        if key in RECENT_ALERTS:
            diff = (now - RECENT_ALERTS[key]).total_seconds()
            if diff < 60:
                print(f"DEDUP skip: {key}")
                return JSONResponse({"status": "duplicate"})
        RECENT_ALERTS[key] = now
        for k in list(RECENT_ALERTS.keys()):
            if (now - RECENT_ALERTS[k]).total_seconds() > 120:
                del RECENT_ALERTS[k]
 
    # Format va gui Telegram
    formatted = format_alert(raw)
    sent = 0
 
    if GROUP_CHAT_ID:
        await tg(GROUP_CHAT_ID, formatted)
        sent += 1
 
    for m in get_active_members():
        await tg(m["user_id"], formatted)
        sent += 1
        await asyncio.sleep(0.05)
 
    # Tao pending order cho MT5 EA
    order_id = None
    if order:
        order_id = str(uuid.uuid4())[:8].upper()
        PENDING_ORDERS[order_id] = {
            **order,
            "order_id":  order_id,
            "ts":        datetime.now().isoformat(),
            "confirmed": False
        }
        print(f"Queued: {order_id} | {order['action']} @ {order['entry']}")
 
    return JSONResponse({
        "status":   "ok",
        "sent":     sent,
        "order_id": order_id
    })
 
# ── MT5 EA: lay lenh moi ────────────────────────────────────
@app.get("/mt5/pending")
async def mt5_pending(
    symbol:        str   = "XAUUSD",
    current_price: float = 0.0,
    max_slip:      int   = 20,
    market_pip:    int   = 10
):
    MAX_SLIP_USD = max_slip * 0.10
 
    for oid, order in list(PENDING_ORDERS.items()):
        if order["confirmed"]:
            continue
 
        if current_price > 0:
            entry  = order["entry"]
            action = order["action"]
            dist   = (
                (current_price - entry)
                if action == "BUY"
                else (entry - current_price)
            )
            if dist > MAX_SLIP_USD:
                order["confirmed"] = True
                msg = (
                    f"SKIP {oid}\n"
                    f"{action} @ {entry:.2f} | Now: {current_price:.2f}\n"
                    f"Chay {dist/0.1:.1f} pip > {max_slip} pip"
                )
                print(msg)
                await tg(ADMIN_ID, msg)
                continue
 
        return JSONResponse(order)
 
    return JSONResponse({})
 
# ── MT5 EA: xac nhan da dat lenh ────────────────────────────
@app.post("/mt5/confirm")
async def mt5_confirm(request: Request):
    data    = await request.json()
    oid     = data.get("order_id", "")
    success = data.get("success", False)
 
    if oid not in PENDING_ORDERS:
        return JSONResponse({"status": "not_found"})
 
    PENDING_ORDERS[oid]["confirmed"] = True
    order = PENDING_ORDERS[oid]
 
    if success:
        msg = (
            f"EA OK\n"
            f"`{order['action']} @ {order['entry']:.2f}`\n"
            f"SL:`{order['sl']:.2f}` TP:`{order['tp']:.2f}`"
        )
    else:
        msg = f"EA FAIL\n`{order['action']} @ {order['entry']:.2f}`"
 
    await tg(ADMIN_ID, msg)
 
    if len(PENDING_ORDERS) > 50:
        oldest = sorted(PENDING_ORDERS.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[:10]:
            del PENDING_ORDERS[k]
 
    return JSONResponse({"status": "confirmed"})
 
# ── MT5 EA: nhan lenh dieu khien ────────────────────────────
@app.get("/mt5/command")
async def mt5_command():
    cmd = EA_COMMAND["cmd"]
    if cmd:
        EA_COMMAND["cmd"] = ""
        print(f"Command sent to EA: {cmd}")
    return JSONResponse({"cmd": cmd})
 
# ── Ping endpoint ────────────────────────────────────────────
@app.get("/ping")
async def ping():
    return {"pong": True, "time": datetime.now().isoformat()}
 
# ── Health check ─────────────────────────────────────────────
@app.get("/")
async def health():
    pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
    return {
        "status":         "running",
        "version":        "3.0",
        "pending_orders": pending,
        "total_received": len(PENDING_ORDERS),
        "ea_command":     EA_COMMAND["cmd"] or "none",
        "time":           datetime.now().isoformat()
    }
 
# ── Startup ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    if APP_URL and BOT_TOKEN:
        try:
            await bot.set_webhook(f"{APP_URL}/telegram-webhook")
            print(f"Webhook set: {APP_URL}/telegram-webhook")
        except Exception as e:
            print(f"Webhook err: {e}")
    asyncio.create_task(keep_alive())
    print("Keep-alive started")
 
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)
 
