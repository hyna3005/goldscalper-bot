"""
Gold Scalper Server v5.0
========================
- Don gian: <= 25 pip -> pass EA, > 25 pip -> skip
- Khong WATCH queue
- Keep-alive 4 phut
- Dieu khien EA qua Telegram
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

app = FastAPI(title="Gold Scalper v5.1")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

async def keep_alive():
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            if APP_URL:
                async with aiohttp.ClientSession() as s:
                    await s.get(f"{APP_URL}/ping", timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            print(f"Keep-alive err: {e}")
        await asyncio.sleep(240)

# ── Database member ───────────────────────────────────────────
DB_FILE = "members.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f: return json.load(f)
    return {"members": {}}

def save_db(data):
    with open(DB_FILE, "w") as f: json.dump(data, f, indent=2, default=str)

def get_active_members():
    db = load_db()
    now = datetime.now()
    return [
        {"user_id": int(uid), "expire_date": datetime.fromisoformat(info["expire_date"])}
        for uid, info in db["members"].items()
        if datetime.fromisoformat(info["expire_date"]) > now
    ]

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

# ── State ─────────────────────────────────────────────────────
PENDING_ORDERS: dict = {}
RECENT_ALERTS:  dict = {}
EA_COMMAND            = {"cmd": ""}

# ── WATCH QUEUE: lenh cho gia hoi quy (TTL 3 phut) ───────────
# Khi gia > 25 pip khoi entry, giu lai 180 giay
# Neu gia hoi ve <= 15 pip thi nha cho EA dat lenh
WATCH_ORDERS:   dict = {}
WATCH_TTL             = 180   # 3 phut
WATCH_TRIGGER_PIP     = 15    # Dat lenh khi gia hoi ve <= 15 pip

async def tg(chat_id, text):
    if not bot or not chat_id: return
    try:
        await bot.send_message(chat_id=int(chat_id), text=str(text), parse_mode="Markdown")
    except TelegramError as e:
        print(f"TG err: {e}")

def parse_order(raw):
    # Bo qua alert quan ly lenh
    skip = ["BREAKEVEN", "CUT LOSS", "TIMEOUT"]
    if any(k in raw for k in skip): return None
    try:
        action = "BUY" if "BUY @" in raw else "SELL" if "SELL @" in raw else None
        if not action: return None
        source = "H1-US" if "[H1-US]" in raw else "M5"

        def get_price(label):
            idx = raw.find(label)
            if idx < 0: return 0.0
            num = ""
            for c in raw[idx+len(label):].lstrip():
                if c.isdigit() or c == ".": num += c
                elif num: break
            return float(num) if num else 0.0

        entry = get_price("BUY @ ") if action == "BUY" else get_price("SELL @ ")
        sl    = get_price("SL ")
        tp    = get_price("TP ")
        score = 0
        for part in raw.split("|"):
            if "pts" in part:
                for tok in part.split():
                    t = tok.replace("pts","")
                    if t.isdigit(): score = int(t); break
        if entry <= 0 or sl <= 0 or tp <= 0: return None
        return {"action": action, "entry": entry, "sl": sl, "tp": tp,
                "source": source, "score": score}
    except Exception as e:
        print(f"Parse err: {e}"); return None

def format_alert(raw):
    # V45: khong forward BREAKEVEN/CUT LOSS/TIMEOUT alert
    skip = ["BREAKEVEN", "CUT LOSS", "TIMEOUT"]
    if any(k in raw for k in skip): return None
    if "DAO CHIEU" in raw: return f"DAO CHIEU\n`{raw}`"
    if "BUY @"     in raw: return f"BUY\n`{raw}`"
    if "SELL @"    in raw: return f"SELL\n`{raw}`"
    return f"`{raw}`"

# ── Telegram bot ──────────────────────────────────────────────
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" not in data: return {"ok": True}

    msg      = data["message"]
    chat_id  = msg["chat"]["id"]
    user_id  = msg["from"]["id"]
    text     = msg.get("text", "").strip()
    is_admin = (user_id == ADMIN_ID)

    if text == "/start":
        reply = (
            "*Gold Scalper v5 - Admin*\n\n"
            "*Member:*\n"
            "`/add <id> <ngay>` `/remove <id>` `/list` `/stats`\n\n"
            "*EA:*\n"
            "`/ea_stop` `/ea_start` `/ea_close` `/ea_status`\n\n"
            "`/test` `/myid`"
        ) if is_admin else "*Gold Scalper Alert Bot*\nGo /myid de lay ID."
        await tg(chat_id, reply)

    elif text == "/myid":
        await tg(chat_id, f"ID: `{user_id}`")

    elif text.startswith("/add") and is_admin:
        parts = text.split()
        if len(parts) == 3:
            try:
                exp = add_member(int(parts[1]), "", int(parts[2]))
                await tg(chat_id, f"Da them `{parts[1]}` - Han: `{exp.strftime('%d/%m/%Y')}`")
            except: await tg(chat_id, "Sai cu phap: `/add <id> <ngay>`")

    elif text.startswith("/remove") and is_admin:
        parts = text.split()
        if len(parts) == 2:
            try:
                remove_member(int(parts[1]))
                await tg(chat_id, f"Da xoa `{parts[1]}`")
            except: await tg(chat_id, "Sai cu phap: `/remove <id>`")

    elif text == "/list" and is_admin:
        members = get_active_members()
        if members:
            lines = [f"*{len(members)} member active:*\n"]
            for m in sorted(members, key=lambda x: x["expire_date"]):
                d = (m["expire_date"] - datetime.now()).days
                e = "🔴" if d<=3 else "🟡" if d<=7 else "🟢"
                lines.append(f"{e} `{m['user_id']}` - Con {d} ngay")
            await tg(chat_id, "\n".join(lines))
        else: await tg(chat_id, "Chua co member nao.")

    elif text == "/stats" and is_admin:
        db = load_db()
        members = get_active_members()
        pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
        await tg(chat_id,
            f"*Stats v5*\n"
            f"Member: `{len(members)}`/`{len(db['members'])}`\n"
            f"Lenh cho EA: `{pending}`\n"
            f"Dang theo doi: `{len(WATCH_ORDERS)}`\n"
            f"Tong nhan: `{len(PENDING_ORDERS)}`")

    elif text == "/test" and is_admin:
        test = "[M5] BUY @ 4525.40 | SL 4520.40 | TP 4531.40 | 67pts [V18] | 14:35 (VN)"
        await tg(chat_id, format_alert(test) or "Test OK")

    elif text == "/ea_stop"   and is_admin: EA_COMMAND["cmd"]="stop";  await tg(chat_id,"STOP sent")
    elif text == "/ea_start"  and is_admin: EA_COMMAND["cmd"]="start"; await tg(chat_id,"START sent")
    elif text == "/ea_close"  and is_admin: EA_COMMAND["cmd"]="close"; await tg(chat_id,"CLOSE sent")
    elif text == "/ea_status" and is_admin:
        pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
        cmd = EA_COMMAND["cmd"] or "running"
        await tg(chat_id,
            f"*EA Status v5*\n"
            f"State: `{cmd.upper()}`\n"
            f"Lenh cho: `{pending}`")

    return {"ok": True}

# ── TradingView webhook ───────────────────────────────────────
@app.post("/webhook/{secret}")
async def receive_alert(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    raw = (await request.body()).decode("utf-8").strip()
    if not raw: raise HTTPException(status_code=400)

    print(f"Alert: {raw[:100]}")

    order = parse_order(raw)

    # Dedup 60 giay
    if order:
        key = f"{order['action']}_{order['entry']}"
        now = datetime.now()
        if key in RECENT_ALERTS and (now - RECENT_ALERTS[key]).total_seconds() < 60:
            return JSONResponse({"status": "duplicate"})
        RECENT_ALERTS[key] = now
        for k in list(RECENT_ALERTS.keys()):
            if (now - RECENT_ALERTS[k]).total_seconds() > 120: del RECENT_ALERTS[k]

    # Format va gui — bo qua BREAKEVEN/CUTLOSS/TIMEOUT
    formatted = format_alert(raw)
    sent = 0
    if formatted:
        if GROUP_CHAT_ID:
            await tg(GROUP_CHAT_ID, formatted); sent += 1
        for m in get_active_members():
            await tg(m["user_id"], formatted); sent += 1
            await asyncio.sleep(0.05)

    # Tao pending order
    order_id = None
    if order:
        order_id = str(uuid.uuid4())[:8].upper()
        PENDING_ORDERS[order_id] = {
            **order, "order_id": order_id,
            "ts": datetime.now().isoformat(), "confirmed": False
        }

    return JSONResponse({"status": "ok", "sent": sent, "order_id": order_id})

# ── MT5 EA: lay lenh (v5.1 — Re-check queue) ────────────────
@app.get("/mt5/pending")
async def mt5_pending(
    symbol:        str   = "XAUUSD",
    current_price: float = 0.0,
    max_slip:      int   = 25
):
    MAX_SLIP_USD     = max_slip * 0.10
    TRIGGER_USD      = WATCH_TRIGGER_PIP * 0.10
    now              = datetime.now()

    # ── Kiem tra WATCH_ORDERS: gia da hoi ve chua? ───────────
    for wid, wo in list(WATCH_ORDERS.items()):
        age = (now - datetime.fromisoformat(wo["watch_ts"])).total_seconds()

        # Het TTL 3 phut -> SKIP that su
        if age > WATCH_TTL:
            del WATCH_ORDERS[wid]
            msg = (f"SKIP {wid} (het 3 phut)\n"
                   f"{wo['action']} @ {wo['entry']:.2f} | Gia khong hoi ve")
            print(msg)
            await tg(ADMIN_ID, msg)
            continue

        if current_price > 0:
            entry  = wo["entry"]
            action = wo["action"]
            dist   = (current_price - entry) if action == "BUY" else (entry - current_price)

            # Gia hoi ve <= 15 pip -> nha lenh cho EA
            if dist <= TRIGGER_USD:
                del WATCH_ORDERS[wid]
                PENDING_ORDERS[wid] = {**wo, "confirmed": False}
                msg = (f"WATCH HIT {wid}\n"
                       f"{action} gia hoi ve @ {current_price:.2f}\n"
                       f"Entry goc: {entry:.2f} | Lech: {dist/0.1:.1f}p")
                print(msg)
                await tg(ADMIN_ID, msg)

    # ── Kiem tra PENDING_ORDERS ───────────────────────────────
    for oid, order in list(PENDING_ORDERS.items()):
        if order["confirmed"]: continue

        if current_price > 0:
            entry  = order["entry"]
            action = order["action"]
            dist   = (current_price - entry) if action == "BUY" else (entry - current_price)

            if dist > MAX_SLIP_USD:
                # Gia qua 25 pip -> chuyen sang WATCH queue (khong SKIP ngay)
                WATCH_ORDERS[oid] = {
                    **order,
                    "watch_ts": now.isoformat()
                }
                order["confirmed"] = True  # Xoa khoi PENDING
                msg = (f"WATCH {oid} (con {WATCH_TTL}s)\n"
                       f"{action} @ {entry:.2f} | Now:{current_price:.2f}\n"
                       f"Lech {dist/0.1:.1f}p > {max_slip}p | Cho gia hoi ve <= {WATCH_TRIGGER_PIP}p")
                print(msg)
                await tg(ADMIN_ID, msg)
                continue

        return JSONResponse(order)

    return JSONResponse({})

# ── MT5 EA: xac nhan ─────────────────────────────────────────
@app.post("/mt5/confirm")
async def mt5_confirm(request: Request):
    data    = await request.json()
    oid     = data.get("order_id", "")
    success = data.get("success", False)
    if oid not in PENDING_ORDERS: return JSONResponse({"status": "not_found"})

    PENDING_ORDERS[oid]["confirmed"] = True
    order = PENDING_ORDERS[oid]
    msg = (f"EA OK\n`{order['action']} @ {order['entry']:.2f}` SL:`{order['sl']:.2f}` TP:`{order['tp']:.2f}`"
           if success else f"EA FAIL\n`{order['action']} @ {order['entry']:.2f}`")
    await tg(ADMIN_ID, msg)

    if len(PENDING_ORDERS) > 100:
        oldest = sorted(PENDING_ORDERS.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[:20]: del PENDING_ORDERS[k]

    return JSONResponse({"status": "confirmed"})

# ── MT5 EA: lenh dieu khien ──────────────────────────────────
@app.get("/mt5/command")
async def mt5_command():
    cmd = EA_COMMAND["cmd"]
    if cmd: EA_COMMAND["cmd"] = ""
    return JSONResponse({"cmd": cmd})

@app.get("/ping")
async def ping():
    return {"pong": True, "time": datetime.now().isoformat()}

@app.get("/")
async def health():
    pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
    return {
        "status": "running", "version": "5.1",
        "pending_orders": pending,
        "watching": len(WATCH_ORDERS),
        "total_received": len(PENDING_ORDERS),
        "ea_command": EA_COMMAND["cmd"] or "none",
        "time": datetime.now().isoformat()
    }

@app.on_event("startup")
async def startup():
    if APP_URL and BOT_TOKEN:
        try:
            await bot.set_webhook(f"{APP_URL}/telegram-webhook")
            print(f"Webhook set OK")
        except Exception as e:
            print(f"Webhook err: {e}")
    asyncio.create_task(keep_alive())
    print("Gold Scalper Server v5.0 started")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)
