"""
Gold Scalper — Combined Server
- /telegram-webhook  : nhận tin nhắn từ Telegram bot
- /webhook/{secret}  : nhận alert từ TradingView
- /mt5/pending       : EA MT5 poll lệnh
- /mt5/confirm       : EA MT5 xác nhận đã đặt lệnh
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
 
app = FastAPI(title="Gold Scalper Bot")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
 
# ─── DATABASE member ───────────────────────────────────────
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
    active = []
    for uid, info in db["members"].items():
        expire = datetime.fromisoformat(info["expire_date"])
        if expire > now:
            active.append({"user_id": int(uid), "username": info.get("username",""), "expire_date": expire})
    return active
 
def add_member(user_id, username, days=30):
    db = load_db()
    now = datetime.now()
    if str(user_id) in db["members"]:
        cur = datetime.fromisoformat(db["members"][str(user_id)]["expire_date"])
        base = max(cur, now)
    else:
        base = now
    expire = base + timedelta(days=days)
    db["members"][str(user_id)] = {"username": username, "added_date": now.isoformat(), "expire_date": expire.isoformat()}
    save_db(db)
    return expire
 
def remove_member(user_id):
    db = load_db()
    db["members"].pop(str(user_id), None)
    save_db(db)
 
# ─── PENDING ORDERS cho MT5 EA ─────────────────────────────
PENDING_ORDERS: dict = {}
 
# ─── TELEGRAM HELPER ───────────────────────────────────────
async def tg_send(chat_id, text):
    if not bot or not chat_id: return
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
    except TelegramError as e:
        print(f"TG error: {e}")
 
# ─── FORMAT ALERT ──────────────────────────────────────────
def format_alert(raw):
    now = datetime.now().strftime("%H:%M:%S")
    is_be  = "BREAKEVEN" in raw
    is_cut = "CUT LOSS"  in raw
    is_to  = "TIMEOUT"   in raw
    is_h1  = "[H1-US]"   in raw
 
    if is_be:   header = "⚡ *BREAKEVEN — Keo SL ve hoa von*"
    elif is_cut: header = "❌ *CAT LENH — Am qua nguong*"
    elif is_to:  header = "⏳ *HET GIO — Xem xet dong lenh*"
    elif is_h1:  header = "🔵 *LENH H1 PHIEN MY*"
    else:        header = ""
 
    hint = ""
    if "BUY @" in raw and not is_be and not is_cut and not is_to:
        hint = "📌 _Dat lenh BUY LIMIT theo gia E_"
    elif "SELL @" in raw and not is_be and not is_cut and not is_to:
        hint = "📌 _Dat lenh SELL LIMIT theo gia E_"
    elif is_be:  hint = "📌 _Keo Stop Loss ve hoa von ngay_"
    elif is_cut: hint = "📌 _Dong lenh Market Order ngay_"
    elif is_to:  hint = "📌 _Xem xet dong lenh_"
 
    parts = ["━"*28]
    if header: parts.append(header)
    parts += [f"`{raw}`", "━"*28, f"🕐 `{now}`"]
    if hint: parts.append(hint)
    return "\n".join(parts)
 
def parse_order(text):
    skip = ["BREAKEVEN","CUT LOSS","TIMEOUT","DAO CHIEU"]
    if any(k in text for k in skip): return None
    try:
        action = "BUY" if "BUY @" in text else "SELL" if "SELL @" in text else None
        if not action: return None
        source = "H1-US" if "[H1-US]" in text else "M5"
 
        def get_price(label):
            idx = text.find(label)
            if idx < 0: return 0.0
            num = ""
            for c in text[idx+len(label):].lstrip():
                if c.isdigit() or c == ".": num += c
                elif num: break
            return float(num) if num else 0.0
 
        entry = get_price("BUY @ ") if action == "BUY" else get_price("SELL @ ")
        sl = get_price("SL ")
        tp = get_price("TP ")
        score = 0
        for part in text.split("|"):
            if "pts" in part:
                for tok in part.split():
                    t = tok.replace("pts","")
                    if t.isdigit(): score = int(t); break
        if entry <= 0 or sl <= 0 or tp <= 0: return None
        return {"action": action, "entry": entry, "sl": sl, "tp": tp, "source": source, "score": score}
    except: return None
 
# ─── TELEGRAM WEBHOOK ──────────────────────────────────────
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" not in data: return {"ok": True}
 
    msg     = data["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text    = msg.get("text", "")
    is_admin = (user_id == ADMIN_ID)
 
    if text == "/start":
        if is_admin:
            reply = ("👋 *Gold Scalper Bot — Admin Panel*\n\n"
                     "`/add <id> <ngay>` — Them member\n"
                     "`/remove <id>` — Xoa member\n"
                     "`/list` — Danh sach member\n"
                     "`/stats` — Thong ke\n"
                     "`/test` — Test alert\n"
                     "`/myid` — Xem ID cua ban")
        else:
            reply = ("👋 *Gold Scalper Alert Bot*\n\n"
                     "Lien he admin de dang ky nhan tin hieu.\n"
                     "Gõ /myid de lay ID cua ban.")
        await tg_send(chat_id, reply)
 
    elif text == "/myid":
        await tg_send(chat_id, f"🆔 Telegram ID cua ban: `{user_id}`")
 
    elif text.startswith("/add") and is_admin:
        parts = text.split()
        if len(parts) == 3:
            try:
                uid = int(parts[1]); days = int(parts[2])
                expire = add_member(uid, "", days)
                await tg_send(chat_id, f"✅ Da them `{uid}` — Han: `{expire.strftime('%d/%m/%Y')}`")
            except: await tg_send(chat_id, "❌ Sai cu phap: `/add <id> <ngay>`")
        else: await tg_send(chat_id, "❌ Sai cu phap: `/add <id> <ngay>`")
 
    elif text.startswith("/remove") and is_admin:
        parts = text.split()
        if len(parts) == 2:
            try:
                uid = int(parts[1]); remove_member(uid)
                await tg_send(chat_id, f"✅ Da xoa member `{uid}`")
            except: await tg_send(chat_id, "❌ Sai cu phap: `/remove <id>`")
 
    elif text == "/list" and is_admin:
        members = get_active_members()
        if members:
            lines = [f"👥 *{len(members)} member active:*\n"]
            for m in sorted(members, key=lambda x: x["expire_date"]):
                d = (m["expire_date"] - datetime.now()).days
                e = "🔴" if d<=3 else "🟡" if d<=7 else "🟢"
                lines.append(f"{e} `{m['user_id']}` — Con {d} ngay")
            await tg_send(chat_id, "\n".join(lines))
        else: await tg_send(chat_id, "📭 Chua co member nao.")
 
    elif text == "/stats" and is_admin:
        db = load_db(); members = get_active_members()
        total = len(db["members"]); active = len(members)
        soon = sum(1 for m in members if (m["expire_date"]-datetime.now()).days <= 7)
        await tg_send(chat_id,
            f"📊 *Thong ke*\nTong: `{total}` | Active: `{active}` | Het han: `{total-active}` | Sap het: `{soon}`")
 
    elif text == "/test" and is_admin:
        test = "[M5] 🟢 BUY @ 4525.40 | SL 4520.40 | TP 4531.40 | ⭐⭐ 67pts [V18] | 14:35 (VN)"
        await tg_send(chat_id, format_alert(test))
 
    return {"ok": True}
 
# ─── TRADINGVIEW WEBHOOK ────────────────────────────────────
@app.post("/webhook/{secret}")
async def receive_alert(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    raw = (await request.body()).decode("utf-8").strip()
    if not raw: raise HTTPException(status_code=400, detail="Empty")
 
    formatted = format_alert(raw)
    sent = 0
 
    # Gửi group chung
    if GROUP_CHAT_ID:
        await tg_send(GROUP_CHAT_ID, formatted)
        sent += 1
 
    # Gửi DM từng member active
    for m in get_active_members():
        await tg_send(m["user_id"], formatted)
        sent += 1
        await asyncio.sleep(0.05)
 
    # Tạo pending order cho MT5 EA
    order = parse_order(raw)
    order_id = None
    if order:
        order_id = str(uuid.uuid4())[:8].upper()
        PENDING_ORDERS[order_id] = {**order, "order_id": order_id, "ts": datetime.now().isoformat(), "confirmed": False}
 
    return JSONResponse({"status": "ok", "sent": sent, "order_id": order_id})
 
# ─── MT5 EA ENDPOINTS ──────────────────────────────────────
@app.get("/mt5/pending")
async def mt5_pending(symbol: str = "XAUUSD"):
    for oid, order in PENDING_ORDERS.items():
        if not order["confirmed"]:
            return JSONResponse(order)
    return JSONResponse({})
 
@app.post("/mt5/confirm")
async def mt5_confirm(request: Request):
    data = await request.json()
    oid = data.get("order_id","")
    if oid not in PENDING_ORDERS: return JSONResponse({"status":"not_found"})
    PENDING_ORDERS[oid]["confirmed"] = True
    return JSONResponse({"status":"confirmed"})
 
# ─── HEALTH CHECK ──────────────────────────────────────────
@app.get("/")
async def health():
    pending = sum(1 for o in PENDING_ORDERS.values() if not o["confirmed"])
    return {"status":"running","pending_orders":pending,"total_received":len(PENDING_ORDERS),"time":datetime.now().isoformat()}
 
# ─── STARTUP — đăng ký Telegram webhook ───────────────────
@app.on_event("startup")
async def startup():
    if APP_URL and BOT_TOKEN:
        try:
            await bot.set_webhook(f"{APP_URL}/telegram-webhook")
            print(f"Webhook set: {APP_URL}/telegram-webhook")
        except Exception as e:
            print(f"Webhook error: {e}")
 
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)
 
