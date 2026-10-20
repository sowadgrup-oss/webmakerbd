#!/usr/bin/env python3
# Web Maker Support Bot (Pro Version) - python-telegram-bot v21+ , firebase-admin
import os, re, time, html, asyncio, logging, secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

import firebase_admin
from firebase_admin import credentials, db
from telegram import (Update, InlineKeyboardButton as Btn,
                      InlineKeyboardMarkup as Markup, LinkPreviewOptions)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("wm-bot-pro")

# ===================== কনফিগ (সরাসরি বসানো হয়েছে) =====================
TOKEN = "8812356448:AAGJ0JASCQA1-excgLYrwIzWyeNOcXDkWyU"
DB_URL = os.environ.get("DB_URL", "https://bd-web-maker-default-rtdb.firebaseio.com")
CRED_FILE = os.environ.get("FIREBASE_CRED", "serviceAccount.json")

# অ্যাডমিন আইডি ও কনফিগারেশন
ADMIN_IDS = [7702408295]
ADMIN_USERNAME = "devlopersowad"
ADMIN_CHAT = 7702408295

SLA_HOURS, SESSION_SEC, MAX_FAILS, LOCK_SEC = 24, 30 * 60, 3, 30 * 60
TZ = ZoneInfo("Asia/Dhaka")

PKG = {"basic": "নরমাল", "plus": "প্লাস", "standard": "মাঝারি", "ultra": "হাই-টেক"}
STAT = {"pending": "⏳ যাচাই চলছে", "approved": "✅ কনফার্মড", "rejected": "❌ পেমেন্ট বাতিল/অস্বীকৃত"}
KIND = {"cancel": "বাতিল / রিফান্ড", "talk": "অ্যাডমিনের সাথে কথা"}
REASONS = ["কাজটি আর চাই না", "ভুল প্যাকেজ নিয়েছি", "ভুল টাকা বা ভুল নম্বর থেকে পাঠিয়েছি", "অন্য কারণ"]

FAQ = (
    "❓ **Web Maker নিয়মকানুন ও পলিসি**\n\n"
    "• পেমেন্ট অবশ্যই সঠিক অ্যামাউন্টে Send Money করতে হবে।\n"
    "• অ্যাডমিন bKash/Nagad অ্যাপে TrxID যাচাই করার পরই অর্ডার কনফার্ম হয়।\n"
    "• ভুল বা ভুয়া তথ্য প্রদান করলে অর্ডার স্বয়ংক্রিয়ভাবে বাতিল হবে এবং রিফান্ড প্রযোজ্য নয়।\n"
    "• কাজ শুরুর পূর্বে অর্ডারের রিফান্ড আবেদন করা যাবে। কাজ শুরু হয়ে গেলে অ্যাডমিনের সিদ্ধান্ত চূড়ান্ত।\n"
    f"• যেকোনো টিকেট খোলার পর সর্বোচ্চ {SLA_HOURS} ঘণ্টার মধ্যে সমাধান করা হয়।"
)

POLICY = {
    "pending": "ℹ️ আপনার পেমেন্ট এখনো যাচাই হয়নি, তাই কাজ শুরু হয়নি। আপনি চাইলে বাতিল ও রিফান্ডের আবেদন করতে পারেন।",
    "approved": "ℹ️ আপনার অর্ডার কনফার্ম হয়ে গেছে। কাজ শুরু হলে রিফান্ডের জন্য অ্যাডমিনের সাথে আলোচনা করুন।",
    "rejected": "ℹ️ পেমেন্ট যাচাই করা যায়নি। সঠিক তথ্য বা প্রমাণসহ আবেদন করুন, অ্যাডমিন যাচাই করে দেখবেন।",
}

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OID_RE = re.compile(r"^WM-[A-Z0-9]{6,12}$")
TRX_RE = re.compile(r"^[A-Z0-9]{8,12}$")

# ===================== হেল্পার ফাংশন =====================
def now_ms(): return int(time.time() * 1000)
def fmt(ms):
    try: return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d/%m/%Y %I:%M %p")
    except Exception: return "-"
def left_txt(ms):
    s = int((ms - now_ms()) / 1000)
    if s <= 0: return "সময়সীমা অতিক্রম করেছে"
    return f"{s // 3600} ঘণ্টা {s % 3600 // 60} মিনিট বাকি"
esc = html.escape

async def dbget(path): return await asyncio.to_thread(lambda: db.reference(path).get())
async def dbset(path, val): await asyncio.to_thread(lambda: db.reference(path).set(val))
async def dbupdate(path, v): await asyncio.to_thread(lambda: db.reference(path).update(v))
async def dbpush(path, val): return await asyncio.to_thread(lambda: db.reference(path).push(val))

async def status_of(oid):
    rv = await dbget(f"review/{oid}")
    return (rv or {}).get("status", "pending")

_last = {}
def too_fast(uid):
    t, p = time.monotonic(), _last.get(uid, 0)
    _last[uid] = t
    return t - p < 0.5

def locked(ctx, uid):
    f = ctx.bot_data.setdefault("fails", {}).get(uid)
    return bool(f and f[1] > time.time())

def add_fail(ctx, uid):
    d = ctx.bot_data.setdefault("fails", {})
    c, u = d.get(uid, (0, 0))
    if u and u <= time.time(): c = 0
    c += 1
    if c >= MAX_FAILS:
        d[uid] = (0, time.time() + LOCK_SEC); return True
    d[uid] = (c, 0); return False

def session(ctx):
    u = ctx.user_data
    if u.get("oid_ok") and u.get("ok_until", 0) > time.time(): return u["oid_ok"]
    u.pop("oid_ok", None); return None

async def say(update, text, kb=None, html_mode=False):
    chat = update.effective_chat
    if not chat: return
    await chat.send_message(
        text, reply_markup=kb,
        parse_mode=ParseMode.HTML if html_mode else None,
        link_preview_options=LinkPreviewOptions(is_disabled=True))

def menu_kb():
    return Markup([
        [Btn("📦 অর্ডারের অবস্থা", callback_data="m:status"), Btn("📜 আমার অর্ডারসমূহ", callback_data="m:history")],
        [Btn("❌ অর্ডার বাতিল / রিফান্ড", callback_data="m:cancel"), Btn("💬 অ্যাডমিনের সাথে কথা", callback_data="m:talk")],
        [Btn("🎫 আমার টিকেটসমূহ", callback_data="m:tix"), Btn("❓ নিয়মকানুন ও FAQ", callback_data="m:faq")],
    ])

def admin_link_kb(oid, tid):
    txt = quote(f"সালাম, আমি ওয়েবসাইট অর্ডার {oid} নিয়ে কথা বলতে চাই। আমার টিকেট আইডি: {tid}")
    return Markup([
        [Btn("👨‍💼 অ্যাডমিনের সাথে সরাসরি চ্যাট করুন", url=f"https://t.me/{ADMIN_USERNAME}?text={txt}")],
        [Btn("🏠 মূল মেনু", callback_data="m:menu")]
    ])

def card(oid, o, st):
    m = "bKash" if o.get("method") == "bkash" else "Nagad"
    c = o.get("createdAt")
    return (f"🧾 <b>অর্ডার আইডি:</b> <code>{oid}</code>\n"
            f"📦 <b>প্যাকেজ:</b> {PKG.get(o.get('package'), o.get('package'))} (৳{o.get('price')})\n"
            f"💳 <b>পেমেন্ট মাধ্যম:</b> {m}\n"
            f"🕒 <b>অর্ডারের সময়:</b> {fmt(c) if isinstance(c, (int, float)) else '-'}\n"
            f"📌 <b>বর্তমান অবস্থা:</b> {STAT.get(st, st)}")

def flags(o, st):
    f = []
    if o.get("amount") != o.get("price"): f.append("টাকার পরিমাণ প্যাকেজের মূল্যের সাথে মেলেনি")
    if st == "pending": f.append("পেমেন্ট যাচাইয়ের অপেক্ষায় রয়েছে")
    if st == "rejected": f.append("পেমেন্ট ত্রুটির কারণে প্রত্যাখ্যাত হয়েছে")
    return f

# ===================== ইউজার ফ্লো =====================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await say(update, "👋 **Web Maker সাপোর্ট সেন্টারে স্বাগতম!**\n\nআপনার যেকোনো প্রয়োজনে আমরা পাশে আছি। নিচ থেকে আপনার কাঙ্ক্ষিত অপশনটি বেছে নিন 👇", menu_kb(), html_mode=True)

async def cmd_abort(update, ctx):
    ctx.user_data.clear()
    await say(update, "❌ বর্তমান প্রসেস বাতিল করা হয়েছে।", menu_kb())

async def begin(update, ctx, intent):
    uid = update.effective_user.id
    if locked(ctx, uid):
        return await say(update, "🔒 বারবার ভুল তথ্য দেওয়ার কারণে সাময়িকভাবে ব্লক করা হয়েছে। ৩০ মিনিট পর আবার চেষ্টা করুন।")
    ctx.user_data["intent"] = intent
    oid = session(ctx)
    if oid: return await act(update, ctx, oid)
    ctx.user_data["state"] = "email"
    await say(update, "🔐 **পরিচয় যাচাইকরণ (Security Verification)**\n\nপ্রথমে আমাদের ওয়েবসাইটে অর্ডার করার সময় যে **ইমেইল (Email)** ব্যবহার করেছিলেন তা এখানে লিখুন:\n\n*(বাতিল করতে /cancel)*", html_mode=True)

async def act(update, ctx, oid):
    o = await dbget(f"orders/{oid}")
    if not o:
        ctx.user_data.pop("oid_ok", None)
        return await say(update, "❌ প্রদত্ত অর্ডারটি ডেটাবেজে পাওয়া যায়নি। সঠিক তথ্য দিয়ে আবার চেষ্টা করুন।", menu_kb())
    st = await status_of(oid)
    intent = ctx.user_data.get("intent", "status")
    other = [Btn("🔄 অন্য অর্ডার চেক", callback_data="x:other"), Btn("🏠 মেনু", callback_data="m:menu")]
    
    if intent == "status":
        extra = {
            "pending": "⏳ আপনার পেমেন্ট অ্যাডমিন যাচাই করছেন। সফলভাবে কনফার্ম হলেই আপনাকে নোটিফিকেশন দেওয়া হবে।",
            "approved": "✅ আপনার অর্ডার সফলভাবে কনফার্ম করা হয়েছে! নিচের বাটনে ক্লিক করে সরাসরি অ্যাডমিনের সাথে কথা বলে কাজের আপডেট নিন।",
            "rejected": "❌ আপনার পেমেন্ট যাচাই করা সম্ভব হয়নি। বিস্তারিত জানতে অ্যাডমিনের সাথে যোগাযোগ করুন।"
        }[st]
        kb = Markup([[Btn("💬 অ্যাডমিনের সাথে কথা বলুন", callback_data="m:talk")],
                     [Btn("❌ অর্ডার বাতিল / রিফান্ড আবেদন", callback_data="m:cancel")], other])
        await say(update, f"✅ **যাচাই সফল হয়েছে**\n\n{card(oid, o, st)}\n\n{extra}", kb, html_mode=True)
    
    elif intent == "cancel":
        kb = Markup([[Btn(r, callback_data=f"r:{i}")] for i, r in enumerate(REASONS)] + [other])
        await say(update, f"✅ **যাচাই সফল হয়েছে**\n\n{card(oid, o, st)}\n\n{POLICY[st]}\n\n📋 **বাতিল বা রিফান্ডের কারণ নির্বাচন করুন:**", kb, html_mode=True)
    else:
        await make_ticket(update, ctx, "talk")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if too_fast(uid): return
    text = (update.message.text or "").strip()[:500]
    st = ctx.user_data.get("state")

    if st == "email":
        e = text.lower()
        if not EMAIL_RE.match(e):
            return await say(update, "❌ সঠিক ইমেইল অ্যাড্রেস লিখুন (যেমন: name@gmail.com)")
        ctx.user_data.update(email=e, state="oid")
        await say(update, "✅ ইমেইল গ্রহণ করা হয়েছে।\n\nএবার আপনার **অর্ডার আইডি** লিখুন (যেমন: `WM-AB12CD3EF`)।\nএটি ওয়েবসাইটের “আমার অর্ডার” পেজে পাবেন।", html_mode=True)

    elif st == "oid":
        o = text.upper().replace(" ", "")
        if not OID_RE.match(o):
            return await say(update, "❌ অর্ডার আইডি সঠিক নয়। এটি 'WM-' দিয়ে শুরু হবে।")
        ctx.user_data.update(oid=o, state="trx")
        await say(update, "✅ শেষ ধাপ: পেমেন্টের **Transaction ID (TrxID)** লিখুন।\n\n🔒 নিরাপত্তার স্বার্থে আপনার এই মেসেজটি স্বয়ংক্রিয়ভাবে মুছে ফেলা হবে।", html_mode=True)

    elif st == "trx":
        trx = text.upper().replace(" ", "")
        try: await update.message.delete()
        except Exception: pass
        
        email, oid = ctx.user_data.get("email"), ctx.user_data.get("oid")
        o = await dbget(f"orders/{oid}") if oid else None
        
        ok = (bool(o) and TRX_RE.match(trx) is not None
              and str(o.get("email", "")).lower() == email
              and str(o.get("trx", "")).upper() == trx)
        
        ctx.user_data["state"] = None
        if not ok:
            if add_fail(ctx, uid):
                return await say(update, "🔒 অনেকবার ভুল তথ্য দেওয়ার কারণে ৩০ মিনিটের জন্য আপনার প্রবেশাধিকার সীমিত করা হলো।")
            return await say(update, "❌ প্রদত্ত তথ্যের সাথে আমাদের ডেটাবেজের তথ্য মিলেনি। দয়া করে ইমেইল, অর্ডার আইডি ও TrxID সঠিক আছে কিনা যাচাই করে আবার চেষ্টা করুন।", menu_kb())
        
        ctx.bot_data.setdefault("fails", {}).pop(uid, None)
        ctx.user_data.update(oid_ok=oid, ok_until=time.time() + SESSION_SEC)
        cur = await status_of(oid)
        
        await dbupdate(f"botLinks/{oid}", {"tg": uid, "user": update.effective_user.username or "", "lastStatus": cur, "at": now_ms()})
        await act(update, ctx, oid)

    elif st == "msg":
        await make_ticket(update, ctx, "cancel", ctx.user_data.get("reason", ""), text)

    else:
        await say(update, "দয়া করে নিচের মেনু থেকে একটি অপশন বেছে নিন 👇", menu_kb())

async def make_ticket(update, ctx, kind, reason="", msg=""):
    u = update.effective_user
    oid = session(ctx)
    if not oid:
        return await say(update, "⌛ সেশনের সময় শেষ হয়ে গেছে। অনুগ্রহ করে আবার মেনু থেকে শুরু করুন।", menu_kb())
    
    o = await dbget(f"orders/{oid}") or {}
    st = await status_of(oid)
    
    for t in (await dbget("tickets") or {}).values():
        if t.get("orderId") == oid and t.get("type") == kind and t.get("status") == "open":
            ctx.user_data["state"] = None
            return await say(update, f"ℹ️ এই অর্ডারের জন্য আপনার একটি টিকেট ইতিমধ্যে খোলা রয়েছে ({t['id']})।\n⏰ {left_txt(t['dueAt'])}", admin_link_kb(oid, t["id"]))
    
    tid = "T-" + secrets.token_hex(3).upper()
    rec = {
        "id": tid, "orderId": oid, "type": kind, "reason": reason, "msg": msg,
        "tgId": u.id, "tgUser": u.username or "", "tgName": u.full_name,
        "email": o.get("email", ""), "status": "open", "warned": 0,
        "createdAt": now_ms(), "dueAt": now_ms() + SLA_HOURS * 3600 * 1000
    }
    await dbset(f"tickets/{tid}", rec)
    await notify_admin(ctx, rec, o, st)
    ctx.user_data["state"] = None
    
    await say(update,
        f"✅ **আপনার অনুরোধ সফলভাবে জমা হয়েছে!**\n\n"
        f"🎫 **টিকেট আইডি:** `{tid}`\n"
        f"⏰ **সমাধানের লক্ষ্যমাত্রা:** {SLA_HOURS} ঘণ্টা ({fmt(rec['dueAt'])})\n\n"
        f"নিচের বাটনে ক্লিক করে সরাসরি অ্যাডমিনের সাথে চ্যাট করতে পারেন 👇",
        admin_link_kb(oid, tid), html_mode=True)

async def notify_admin(ctx, t, o, st):
    fl = flags(o, st)
    text = (f"🎫 <b>নতুন সাপোর্ট টিকেট [{esc(t['id'])}]</b> ({KIND[t['type']]})\n"
            f"🧾 অর্ডার: <code>{esc(t['orderId'])}</code>\n"
            f"📧 ইমেইল: {esc(t['email'])}\n"
            f"👤 ইউজার: <a href=\"tg://user?id={t['tgId']}\">{esc(t['tgName'])}</a>"
            f"{' @' + esc(t['tgUser']) if t['tgUser'] else ''}\n"
            f"📦 প্যাকেজ: {esc(PKG.get(o.get('package'), str(o.get('package'))))} ৳{o.get('price')} | প্রদত্ত টাকা: ৳{o.get('amount')} | মাধ্যম: {esc(str(o.get('method')))}\n"
            f"🔢 TrxID: <code>{esc(str(o.get('trx')))}</code> | প্রেরক: {esc(str(o.get('sender')))}\n"
            f"📌 স্ট্যাটাস: {STAT.get(st, st)}\n")
    if fl: text += "⚠️ ফ্ল্যাগ: " + "; ".join(fl) + "\n"
    if t["reason"]: text += f"❓ কারণ: {esc(t['reason'])}\n"
    if t["msg"]: text += f"✍️ বার্তা: {esc(t['msg'])}\n"
    text += f"⏰ সময়সীমা: {fmt(t['dueAt'])}"
    
    kb = Markup([[Btn("✅ সমাধান করুন", callback_data=f"a:res:{t['id']}"), Btn("🚫 বন্ধ করুন", callback_data=f"a:dis:{t['id']}")]])
    try:
        await ctx.bot.send_message(ADMIN_CHAT, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.error("admin notify failed: %s", e)

async def my_tickets(update, ctx):
    uid = update.effective_user.id
    mine = [t for t in (await dbget("tickets") or {}).values() if t.get("tgId") == uid]
    mine.sort(key=lambda t: t.get("createdAt", 0), reverse=True)
    if not mine: return await say(update, "📭 আপনার কোনো টিকেট ডেটাবেজে পাওয়া যায়নি।", menu_kb())
    
    lines = []
    for t in mine[:5]:
        s = {"open": "🟡 খোলা (চলমান)", "resolved": "✅ সমাধান সম্পন্ন", "dismissed": "⚪ বন্ধ করা হয়েছে"}.get(t["status"], t["status"])
        lines.append(f"🎫 **{t['id']}** • {KIND.get(t['type'], t['type'])}\n └ অর্ডার: `{t['orderId']}` • {s}")
    await say(update, "📋 **আপনার সাম্প্রতিক টিকেটসমূহ:**\n\n" + "\n\n".join(lines), menu_kb(), html_mode=True)

async def user_order_history(update, ctx):
    oid = session(ctx)
    if not oid:
        ctx.user_data["intent"] = "status"
        ctx.user_data["state"] = "email"
        return await say(update, "📜 অর্ডার হিস্ট্রি দেখতে প্রথমে আপনার পরিচয় (ইমেইল ও অর্ডার আইডি দিয়ে) ভেরিফাই করুন।\n\nপ্রথমে আপনার ইমেইল লিখুন:")
    o = await dbget(f"orders/{oid}")
    st = await status_of(oid)
    if not o: return await say(update, "অর্ডার ডেটা পাওয়া যায়নি।", menu_kb())
    await say(update, f"📜 **আপনার ভেরিফাইড অর্ডার বিবরণী:**\n\n{card(oid, o, st)}", menu_kb(), html_mode=True)

# ===================== বাটন কলব্যাক হ্যান্ডলার =====================
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d, uid = q.data or "", q.from_user.id

    if d.startswith("a:"):
        if uid not in ADMIN_IDS:
            return await q.answer("আপনার এই কাজের অনুমতি নেই!", show_alert=True)
        _, a, tid = d.split(":", 2)
        t = await close_ticket(ctx, tid, "resolved" if a == "res" else "dismissed", "", uid)
        await q.answer("সফলভাবে আপডেট হয়েছে" if t and not t.get("_already") else "টিকেটটি ইতিমধ্যে বন্ধ বা অনুপস্থিত")
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    if d.startswith("fb:"):
        rating = d.split(":")[1]
        await q.answer("ধন্যবাদ! আপনার মূল্যবান ফিডব্যাক সংরক্ষিত হয়েছে। ❤️", show_alert=True)
        try: await q.edit_message_text(f"⭐ রেটিং সাবমিট করার জন্য ধন্যবাদ ({rating}/5 স্টার)।")
        except Exception: pass
        return

    await q.answer()
    if too_fast(uid): return

    if d == "m:menu":
        ctx.user_data["state"] = None
        await say(update, "মূল মেনু 👇", menu_kb())
    elif d in ("m:status", "m:cancel", "m:talk"):
        await begin(update, ctx, d.split(":")[1])
    elif d == "m:history":
        await user_order_history(update, ctx)
    elif d == "m:faq":
        await say(update, FAQ, menu_kb(), html_mode=True)
    elif d == "m:tix":
        await my_tickets(update, ctx)
    elif d == "x:other":
        ctx.user_data.pop("oid_ok", None)
        await begin(update, ctx, ctx.user_data.get("intent", "status"))
    elif d.startswith("r:"):
        if not session(ctx):
            return await say(update, "⌛ সেশনের সময় শেষ। আবার শুরু করুন।", menu_kb())
        try: ctx.user_data["reason"] = REASONS[int(d[2:])]
        except Exception: return
        ctx.user_data["state"] = "msg"
        await say(update, f"নির্বাচিত কারণ: **{ctx.user_data['reason']}**\n\n✍️ এবার আপনার সমস্যা বা অভিযোগ সম্পর্কে বিস্তারিত লিখুন (সর্বোচ্চ ৫০০ অক্ষর)।\n\n*(বাতিল করতে /cancel)*", html_mode=True)

async def close_ticket(ctx, tid, status, note, by):
    t = await dbget(f"tickets/{tid}")
    if not t: return None
    if t.get("status") != "open":
        t["_already"] = True; return t
    
    await dbupdate(f"tickets/{tid}", {"status": status, "closedAt": now_ms(), "closedBy": by, "note": note})
    
    msg = (f"✅ আপনার টিকেট **{tid}** সফলভাবে সমাধান করা হয়েছে।" if status == "resolved" else f"ℹ️ আপনার টিকেট **{tid}** বন্ধ করা হয়েছে।")
    if note: msg += f"\n📝 **অ্যাডমিনের নোট:** {note}"
    
    fb_markup = Markup([[Btn("⭐ ১", callback_data="fb:1"), Btn("⭐ ২", callback_data="fb:2"), Btn("⭐ ৩", callback_data="fb:3"), Btn("⭐ ৪", callback_data="fb:4"), Btn("⭐ ৫", callback_data="fb:5")]])
    
    try: 
        await ctx.bot.send_message(t["tgId"], msg + "\n\nআমাদের সাপোর্ট সেবা আপনার কেমন লাগলো রেটিং দিন 👇", reply_markup=fb_markup, parse_mode=ParseMode.HTML)
    except Exception as e: 
        log.warning("user notify failed: %s", e)
    return t

# ===================== অ্যাডমিন কমান্ডসমূহ =====================
def admin_only(fn):
    async def w(update, ctx):
        if update.effective_user.id not in ADMIN_IDS: return
        return await fn(update, ctx)
    return w

@admin_only
async def cmd_tickets(update, ctx):
    tix = [t for t in (await dbget("tickets") or {}).values() if t.get("status") == "open"]
    if not tix: return await say(update, "✅ বর্তমানে কোনো খোলা টিকেট নেই।")
    tix.sort(key=lambda t: t["dueAt"])
    await say(update, "🟡 **খোলা টিকেটসমূহ:**\n\n" + "\n".join(
        f"• `{t['id']}` | {KIND.get(t['type'])} | অর্ডার: `{t['orderId']}` | {left_txt(t['dueAt'])}" for t in tix[:30]), html_mode=True)

async def _close_cmd(update, ctx, status):
    if not ctx.args: return await say(update, "ব্যবহারবিধি: `/resolve T-XXXXXX [নোট]`", html_mode=True)
    t = await close_ticket(ctx, ctx.args[0].upper(), status, " ".join(ctx.args[1:])[:300], update.effective_user.id)
    await say(update, "❌ টিকেট পাওয়া যায়নি।" if not t else ("⚠️ টিকেটটি আগেই বন্ধ ছিল।" if t.get("_already") else "✅ সফলভাবে সম্পন্ন হয়েছে এবং ইউজারকে জানানো হয়েছে।"))

@admin_only
async def cmd_resolve(update, ctx): await _close_cmd(update, ctx, "resolved")
@admin_only
async def cmd_dismiss(update, ctx): await _close_cmd(update, ctx, "dismissed")

@admin_only
async def cmd_order(update, ctx):
    if not ctx.args: return await say(update, "ব্যবহারবিধি: `/order WM-XXXXXXXX`", html_mode=True)
    oid = ctx.args[0].upper()
    o = await dbget(f"orders/{oid}")
    if not o: return await say(update, "❌ অর্ডার ডেটাবেজে পাওয়া যায়নি।")
    st = await status_of(oid)
    fl = flags(o, st)
    await say(update, card(oid, o, st) + f"\n👤 গ্রাহক: {o.get('name')} | {o.get('contact')}\n📧 ইমেইল: {o.get('email')}\n🔢 TrxID: `{o.get('trx')}` | প্রেরক: {o.get('sender')} | দাবি: ৳{o.get('amount')}" +
              ("\n⚠️ ফ্ল্যাগ: " + "; ".join(fl) if fl else ""), html_mode=True)

@admin_only
async def cmd_pending(update, ctx):
    orders, rv = await dbget("orders") or {}, await dbget("review") or {}
    p = [(k, v) for k, v in orders.items() if (rv.get(k) or {}).get("status", "pending") == "pending"]
    if not p: return await say(update, "✅ কোনো পেন্ডিং অর্ডার নেই।")
    p.sort(key=lambda x: x[1].get("createdAt", 0))
    await say(update, f"⏳ **পেন্ডিং অর্ডারসমূহ ({len(p)}টি):**\n\n" + "\n".join(
        f"• `{k}` | {v.get('name')} | ৳{v.get('price')} | Trx: `{v.get('trx')}`" for k, v in p[:20]), html_mode=True)

async def _set_order(update, ctx, status):
    if not ctx.args: return await say(update, "ব্যবহারবিধি: `/confirm WM-XXXXXXXX` বা `/deny WM-XXXXXXXX`", html_mode=True)
    oid = ctx.args[0].upper()
    if not await dbget(f"orders/{oid}"): return await say(update, "❌ অর্ডার পাওয়া যায়নি।")
    await dbset(f"review/{oid}", {"status": status, "at": now_ms()})
    await say(update, f"✅ অর্ডার `{oid}` এর স্ট্যাটাস পরিবর্তন করে **{STAT[status]}** করা হয়েছে। ইউজার স্বয়ংক্রিয় নোটিফিকেশন পেয়ে যাবেন।", html_mode=True)

@admin_only
async def cmd_confirm(update, ctx): await _set_order(update, ctx, "approved")
@admin_only
async def cmd_deny(update, ctx): await _set_order(update, ctx, "rejected")

@admin_only
async def cmd_broadcast(update, ctx):
    if not ctx.args:
        return await say(update, "ব্যবহারবিধি: `/broadcast আপনার মেসেজ এখানে লিখুন`", html_mode=True)
    msg = "📢 **বিশেষ ঘোষণা:**\n\n" + " ".join(ctx.args)
    links = await dbget("botLinks") or {}
    sent = 0
    for l in links.values():
        try:
            await ctx.bot.send_message(l["tg"], msg, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await say(update, f"✅ সফলভাবে {sent} জন ইউজারের কাছে ব্রডকাস্ট পাঠানো হয়েছে।")

@admin_only
async def cmd_stats(update, ctx):
    orders, rv = await dbget("orders") or {}, await dbget("review") or {}
    c = {"pending": 0, "approved": 0, "rejected": 0}
    for k in orders: 
        st = (rv.get(k) or {}).get("status", "pending")
        c[st] = c.get(st, 0) + 1
    open_t = sum(1 for t in (await dbget("tickets") or {}).values() if t.get("status") == "open")
    await say(update, f"📊 **বটের পরিসংখ্যান (Statistics):**\n\n• মোট অর্ডার: `{len(orders)}`\n• ⏳ পেন্ডিং: `{c['pending']}`\n• ✅ কনফার্মড: `{c['approved']}`\n• ❌ রিজেক্টেড: `{c['rejected']}`\n• 🎫 খোলা টিকেট: `{open_t}`", html_mode=True)

# ===================== ব্যাকগ্রাউন্ড জবসমূহ =====================
async def job_status(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        links = await dbget("botLinks") or {}
        if not links: return
        rv = await dbget("review") or {}
        for oid, l in links.items():
            cur = (rv.get(oid) or {}).get("status", "pending")
            if cur == l.get("lastStatus"): continue
            await dbupdate(f"botLinks/{oid}", {"lastStatus": cur})
            
            msg = {
                "approved": f"🎉 **সুসংবাদ!** আপনার অর্ডার `{oid}` সফলভাবে কনফার্ম করা হয়েছে!\nঅ্যাডমিনের সাথে কথা বলে আপনার ওয়েবসাইটের কাজ শুরু করে দিন।",
                "rejected": f"❌ দুঃখিত, আপনার অর্ডার `{oid}` এর পেমেন্ট যাচাই করা যায়নি। কোনো সমস্যা থাকলে মেনু থেকে অ্যাডমিনের সাথে যোগাযোগ করুন।",
                "pending": f"⏳ আপনার অর্ডার `{oid}` স্ট্যাটাস আপডেট হয়ে পুনরায় যাচাইয়ের অপেক্ষায় রয়েছে।"
            }[cur]
            
            try: await ctx.bot.send_message(l["tg"], msg, reply_markup=menu_kb(), parse_mode=ParseMode.HTML)
            except Exception as e: log.warning("status notify failed: %s", e)
    except Exception as e:
        log.error("job_status error: %s", e)

async def job_sla(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        for tid, t in (await dbget("tickets") or {}).items():
            if t.get("status") != "open": continue
            rem, w = t["dueAt"] - now_ms(), t.get("warned", 0)
            if 0 < rem <= 6 * 3600 * 1000 and w < 1:
                await dbupdate(f"tickets/{tid}", {"warned": 1})
                await ctx.bot.send_message(ADMIN_CHAT, f"⏰ **সতর্কতা:** টিকেট `{tid}` ({t['orderId']}) এর সময়সীমা শেষ হতে মাত্র {left_txt(t['dueAt'])} বাকি। দ্রুত সমাধান করুন।", parse_mode=ParseMode.HTML)
            elif rem <= 0 and w < 2:
                await dbupdate(f"tickets/{tid}", {"warned": 2})
                await ctx.bot.send_message(ADMIN_CHAT, f"🚨 **জরুরি:** টিকেট `{tid}` ({t['orderId']}) এর নির্ধারিত {SLA_HOURS} ঘণ্টা সময় পার হয়ে গেছে!", parse_mode=ParseMode.HTML)
                try: await ctx.bot.send_message(t["tgId"], f"🙏 আপনার টিকেট `{tid}` সমাধানে কিছুটা দেরি হওয়ায় আমরা আন্তরিকভাবে দুঃখিত। অ্যাডমিনকে জরুরি নোটিশ পাঠানো হয়েছে।", parse_mode=ParseMode.HTML)
                except Exception: pass
    except Exception as e:
        log.error("job_sla error: %s", e)

async def on_error(update, ctx):
    log.error("Telegram Bot Error: %s", ctx.error)

# ===================== মেইন ফাংশন =====================
def main():
    firebase_admin.initialize_app(credentials.Certificate(CRED_FILE), {"databaseURL": DB_URL})
    app = Application.builder().token(TOKEN).build()
    pv = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=pv))
    app.add_handler(CommandHandler("cancel", cmd_abort, filters=pv))

    for name, fn in [
        ("tickets", cmd_tickets), ("resolve", cmd_resolve), ("dismiss", cmd_dismiss),
        ("order", cmd_order), ("pending", cmd_pending), ("confirm", cmd_confirm),
        ("deny", cmd_deny), ("broadcast", cmd_broadcast), ("stats", cmd_stats)
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(pv & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(job_status, interval=60, first=15)
    app.job_queue.run_repeating(job_sla, interval=600, first=60)

    log.info("Web Maker Support Pro Bot Started Successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
