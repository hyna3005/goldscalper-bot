import re
"""
Gold Scalper Server v5.3
========================
P1: RegEx parse - fix bug nuot lenh
P2: Loc vung nhieu < 50 pip
P3: Watch queue + gio re-check
P4: Keep-alive, dieu khien EA
"""

import os, re, json, uuid, asyncio
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

app = FastAPI(title="Gold Scalper v5.3.1")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

# ── Keep-alive ────────────────────────────────────────────────
async def keep_alive():
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            if APP_URL:
                async with aiohttp.ClientSession() as s:
                    await s.get(f"{APP_URL}/ping",
                                timeout=aiohttp.ClientTimeout(total=10))
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
        {"user_id": int(uid),
         "expire_date": datetime.fromisoformat(info["expire_date"])}
        for uid, info in db["members"].items()
        if datetime.fromisoformat(info["expire_date"]) > now
    ]

def add_member(user_id, username, days=30):
    db = load_db()
    now = datetime.now()
    if str(user_id) in db["members"]:
        cur  = datetime.fromisoformat(db["members"][str(user_id)]["expire_date"])
        base = max(cur, now)
    else:
        base = now
    expire = base + timedelta(days=days)
    db["members"][str(user_id)] = {
        "username":    username,
        "added_date":  now.isoformat(),
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

# Watch queue: lenh cho gia hoi quy (TTL 3 phut)
WATCH_ORDERS:     dict = {}
WATCH_TTL               = 180   # 3 phut
WATCH_TRIGGER_PIP       = 15    # Dat lenh khi gia hoi ve <= 15 pip

# Loc nhieu: lich su lenh 15 phut gan nhat
RECENT_ORDERS:    list = []
MIN_POS_DIST_PIP        = 50    # Khoang cach entry toi thieu giua 2 lenh nguoc chieu
MIN_ORDER_INTERVAL      = 900   # 15 phut (giay)

# ── Telegram ─────────────────────────────────────────────────
async def tg(chat_id, text):
    if not bot or not chat_id: return
    try:
        await bot.send_message(
            chat_id=int(chat_id), text=str(text), parse_mode="Markdown")
    except TelegramError as e:
        print(f"TG err: {e}")

# ── P1: Parse alert bang RegEx ────────────────────────────────
def parse_order(raw):
    """
    Parse alert tu V45.1:
    - [M5] BUY @ 4468.12 | SL 4462.12 | TP 4475.12 | 68pts [V18]
    - [DAO CHIEU - DONG BUY] -> SELL @ 4466.54 | SL ... | TP ...
    - DONG BUY/SELL @ price (khong co SL/TP -> skip)
    """
    skip = ["BREAKEVEN", "CUT LOSS", "TIMEOUT"]
    if any(k in raw for k in skip):
        return None
    # Bo qua DONG don thuan (khong co SL/TP)
    if "DONG " in raw and "SL" not in raw:
        return None
    try:
        working = raw
        # Xu ly DAO CHIEU: lay phan sau "->" de lay chieu MOI
        if "->" in raw:
            parts = raw.split("->")
            if len(parts) > 1:
                working = parts[-1].strip()

        # Nhan dien chieu lenh tu phan can xu ly
        action = "BUY"  if re.search(r'\bBUY\b',  working) else \
                 "SELL" if re.search(r'\bSELL\b', working) else None
        if not action:
            return None

        source = "H1-US" if "[H1-US]" in raw else "M5"

        # RegEx linh hoat: BUY @, SELL @
        entry_m = re.search(r"(?:BUY|SELL)\s*@\s*([\d.]+)", working)
        sl_m    = re.search(r"\bSL[:\s]+([\d.]+)", working)
        tp_m    = re.search(r"\bTP[:\s]+([\d.]+)", working)
        score_m = re.search(r"(\d+)\s*pts", raw)

        if not entry_m or not sl_m or not tp_m:
            print(f"RegEx parse fail: {raw[:80]}")
            return None

        entry = float(entry_m.group(1))
        sl    = float(sl_m.group(1))
        tp    = float(tp_m.group(1))
        score = int(score_m.group(1)) if score_m else 0

        if entry <= 0 or sl <= 0 or tp <= 0:
            return None

        return {
            "action": action, "entry": entry,
            "sl": sl, "tp": tp,
            "source": source, "score": score
        }
    except Exception as e:
        print(f"Parse err: {e}")
        return None

# ── P2: Loc vung nhieu < 50 pip ──────────────────────────────
def is_noise_zone(new_order):
    """
    Tra ve True neu lenh moi nam trong vung nhieu:
    - Nguoc chieu lenh truoc do trong 15 phut
    - Khoang cach entry < 50 pip
    """
    now = datetime.now()
    # Don dep lenh cu qua 15 phut
    global RECENT_ORDERS
    RECENT_ORDERS = [
        o for o in RECENT_ORDERS
        if (now - datetime.fromisoformat(o["ts"])).total_seconds() < MIN_ORDER_INTERVAL
    ]

    for prev in RECENT_ORDERS:
        # Chi check lenh nguoc chieu
        if prev["action"] == new_order["action"]:
            continue
        dist_pip = abs(new_order["entry"] - prev["entry"]) / 0.1
        if dist_pip < MIN_POS_DIST_PIP:
            return True, prev["action"], round(dist_pip, 1)

    return False, None, 0.0

def record_order(order):
    """Luu lenh vao lich su 15 phut"""
    RECENT_ORDERS.append({
        "action": order["action"],
        "entry":  order["entry"],
        "ts":     datetime.now().isoformat()
    })

def format_alert(raw):
    """Format tin nhan Telegram — bo qua alert quan ly lenh"""
    skip = ["BREAKEVEN", "CUT LOSS", "TIMEOUT"]
    if any(k in raw for k in skip):
        return None
    if "DAO CHIEU" in raw: return f"DAO CHIEU\n`{raw}`"
    if "BUY"       in raw: return f"BUY\n`{raw}`"
    if "SELL"      in raw: return f"SELL\n`{raw}`"
    return f"`{raw}`"

# ── Telegram bot commands ─────────────────────────────────────
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
        reply = (
            "*Gold Scalper v5.3 - Admin*\n\n"
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
                await tg(chat_id,
                    f"Da them `{parts[1]}` - Han: `{exp.strftime('%d/%m/%Y')}`")
            except:
                await tg(chat_id, "Sai cu phap: `/add <id> <ngay>`")

    elif text.startswith("/remove") and is_admin:
        parts = text.split()
        if len(parts) == 2:
            try:
                remove_member(int(parts[1]))
                await tg(chat_id, f"Da xoa `{parts[1]}`")
            except:
                await tg(chat_id, "Sai cu phap: `/remove <id>`")

    elif text == "/list" and is_admin:
        members = get_active_members()
        if members:
            lines = [f"*{len(members)} member active:*\n"]
            for m in sorted(members, key=lambda x: x["expire_date"]):
                d = (m["expire_date"] - datetime.now()).days
                e = "🔴" if d<=3 else "🟡" if d<=7 else "🟢"
                lines.append(f"{e} `{m['user_id']}` - Con {d} ngay")
            await tg(chat_id, "\n".join(lines))
        else:
            await tg(chat_id, "Chua co member nao.")

    elif text == "/stats" and is_admin:
        db      = load_db()
        members = get_active_members()
        pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
        await tg(chat_id,
            f"*Stats v5.3*\n"
            f"Member: `{len(members)}`/`{len(db['members'])}`\n"
            f"Lenh cho EA: `{pending}`\n"
            f"Tong nhan: `{len(PENDING_ORDERS)}`")

    elif text == "/test" and is_admin:
        test = "[M5] BUY @ 4525.40 | SL 4520.40 | TP 4531.40 | 67pts [V18] | 14:35 (VN)"
        parsed = parse_order(test)
        await tg(chat_id,
            f"Test parse:\n`{test}`\n\nKet qua: `{parsed}`")

    elif text == "/ea_stop"   and is_admin:
        EA_COMMAND["cmd"] = "stop"
        await tg(chat_id, "STOP sent to EA")
    elif text == "/ea_start"  and is_admin:
        EA_COMMAND["cmd"] = "start"
        await tg(chat_id, "START sent to EA")
    elif text == "/ea_close"  and is_admin:
        EA_COMMAND["cmd"] = "close"
        await tg(chat_id, "CLOSE sent to EA")
    elif text == "/ea_status" and is_admin:
        pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
        cmd = EA_COMMAND["cmd"] or "running"
        await tg(chat_id,
            f"*EA Status*\n"
            f"State: `{cmd.upper()}`\n"
            f"Lenh cho: `{pending}`\n"
            f"Watch: `{len(WATCH_ORDERS)}`")

    return {"ok": True}

# ── TradingView webhook ───────────────────────────────────────
@app.post("/webhook/{secret}")
async def receive_alert(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    raw = (await request.body()).decode("utf-8").strip()
    if not raw:
        raise HTTPException(status_code=400)

    print(f"Alert: {raw[:120]}")

    order = parse_order(raw)

    if order:
        # Dedup 60 giay
        key = f"{order['action']}_{order['entry']}"
        now = datetime.now()
        if key in RECENT_ALERTS and \
           (now - RECENT_ALERTS[key]).total_seconds() < 60:
            return JSONResponse({"status": "duplicate"})
        RECENT_ALERTS[key] = now
        for k in list(RECENT_ALERTS.keys()):
            if (now - RECENT_ALERTS[k]).total_seconds() > 120:
                del RECENT_ALERTS[k]

        # P2: Kiem tra vung nhieu
        is_noise, prev_dir, dist_p = is_noise_zone(order)
        if is_noise:
            oid = str(uuid.uuid4())[:8].upper()
            msg = (f"SKIP {oid} | Vung nhieu hep < {MIN_POS_DIST_PIP}p\n"
                   f"{order['action']} @ {order['entry']:.2f}\n"
                   f"Lenh {prev_dir} truoc do cach {dist_p:.1f}p < {MIN_POS_DIST_PIP}p")
            print(msg)
            await tg(ADMIN_ID, msg)
            return JSONResponse({"status": "noise_filtered", "dist_pip": dist_p})

        # Ghi vao lich su
        record_order(order)

        # Tao pending order
        order_id = str(uuid.uuid4())[:8].upper()
        PENDING_ORDERS[order_id] = {
            **order,
            "order_id":  order_id,
            "ts":        now.isoformat(),
            "confirmed": False
        }
        print(f"Queued: {order_id} | {order['action']} @ {order['entry']}")

    # Gui Telegram (bo qua BREAKEVEN/CUTLOSS/TIMEOUT)
    formatted = format_alert(raw)
    sent = 0
    if formatted:
        if GROUP_CHAT_ID:
            await tg(GROUP_CHAT_ID, formatted)
            sent += 1
        for m in get_active_members():
            await tg(m["user_id"], formatted)
            sent += 1
            await asyncio.sleep(0.05)

    return JSONResponse({
        "status":   "ok",
        "sent":     sent,
        "order_id": order.get("order_id") if order else None
    })

# ── MT5 EA: lay lenh ─────────────────────────────────────────
# Server chi pass lenh xuong EA, khong tu skip hay watch
# EA tu xu ly Watch Zone + Momentum Entry (OnTick)
@app.get("/mt5/pending")
async def mt5_pending(symbol: str = "XAUUSD"):
    """
    Don gian: tra ve lenh dau tien chua confirmed.
    Moi logic skip/watch/entry giao hoan toan cho EA xu ly.
    """
    for oid, order in list(PENDING_ORDERS.items()):
        if not order["confirmed"]:
            return JSONResponse(order)
    return JSONResponse({})

# ── MT5 EA: xac nhan ─────────────────────────────────────────
@app.post("/mt5/confirm")
async def mt5_confirm(request: Request):
    data    = await request.json()
    oid     = data.get("order_id", "")
    success = data.get("success", False)

    if oid not in PENDING_ORDERS:
        return JSONResponse({"status": "not_found"})

    PENDING_ORDERS[oid]["confirmed"] = True
    order = PENDING_ORDERS[oid]
    msg = (
        f"EA OK\n`{order['action']} @ {order['entry']:.2f}`\n"
        f"SL:`{order['sl']:.2f}` TP:`{order['tp']:.2f}`"
        if success else
        f"EA FAIL\n`{order['action']} @ {order['entry']:.2f}`"
    )
    await tg(ADMIN_ID, msg)

    if len(PENDING_ORDERS) > 100:
        oldest = sorted(PENDING_ORDERS.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[:20]:
            del PENDING_ORDERS[k]

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
        "status":         "running",
        "version":        "5.3.1",
        "pending_orders": pending,
        "total_received": len(PENDING_ORDERS),
        "ea_command":     EA_COMMAND["cmd"] or "none",
        "time":           datetime.now().isoformat()
    }

@app.on_event("startup")
async def startup():
    if APP_URL and BOT_TOKEN:
        try:
            await bot.set_webhook(f"{APP_URL}/telegram-webhook")
            print(f"Webhook OK")
        except Exception as e:
            print(f"Webhook err: {e}")
    asyncio.create_task(keep_alive())
    print("Gold Scalper Server v5.3 started")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)
