#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeraBox Direct-Link Bot (aiogram v3, single-file)
=================================================

- One-file bot: just set BOT_TOKEN in .env and run.
- Replaces Telethon with aiogram (v3.7+).
- Robust TeraBox resolver (no cookie required) using /share/list API.
- 10+ inline features, owner admin panel, batch processing, rate limit, etc.

QUICK START
-----------
1) Create a .env file next to this script:
   BOT_TOKEN=123456:ABC...

2) Install deps (Python 3.10+ recommended):
   pip install -U aiogram==3.7.0 httpx==0.27.2 python-dotenv==1.0.1

   # Optional (Linux): faster event loop
   pip install -U uvloop && python3 -c "import uvloop,asyncio; asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())"

3) Run it:
   python3 bot.py

NOTES
-----
- No extra env needed; only BOT_TOKEN. Owner is hardcoded below as OWNER_ID.
- All replies use inline keyboards.
- Downloads are optional; by default the bot returns a direct link. Server-download is limited to ~2GB.
- If a link is truly private or TeraBox changes, you'll get a friendly error instead of a crash.
"""

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ====== Configuration ======
# Only the token is kept in .env as requested.
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise SystemExit("ERROR: Set BOT_TOKEN in .env first.")

# Make sure the owner (you) never hits any restriction.
OWNER_ID = 7940894807  # per user request

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("terabox-bot")

# SQLite (lightweight settings & user list)
DB_PATH = "bot_data.sqlite3"

# Per-user rate limit (seconds). Owner is exempt.
RATE_LIMIT_S = 10

# Download limit for server-mirror (bytes)
MIRROR_MAX_BYTES = 2_000_000_000  # ~1.86 GiB (Bot API allows 2GB+, but be safe)

# HTTP client defaults
UA = (
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0 Mobile Safari/537.36"
)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "User-Agent": UA,
}

# ====== Minimal persistence ======
def db_init() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            first_seen INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            user_id INTEGER PRIMARY KEY,
            auto_short INTEGER DEFAULT 0,
            auto_mirror INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ratelimit(
            user_id INTEGER PRIMARY KEY,
            last_ts INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()

def db_upsert_user(u: Message) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users(user_id, first_name, last_name, username, first_seen)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username
    """, (u.from_user.id, u.from_user.first_name or "", u.from_user.last_name or "", u.from_user.username or "", int(time.time())))
    # Ensure settings row exists
    cur.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (u.from_user.id,))
    con.commit()
    con.close()

def db_get_settings(uid: int) -> Tuple[bool, bool]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT auto_short, auto_mirror FROM settings WHERE user_id=?", (uid,))
    row = cur.fetchone()
    con.close()
    if not row:
        return False, False
    return bool(row[0]), bool(row[1])

def db_toggle(uid: int, field: str) -> bool:
    assert field in ("auto_short", "auto_mirror")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(f"SELECT {field} FROM settings WHERE user_id=?", (uid,))
    row = cur.fetchone()
    val = 0 if (row and row[0]) else 1
    cur.execute(f"UPDATE settings SET {field}=? WHERE user_id=?", (val, uid))
    con.commit()
    con.close()
    return bool(val)

def db_should_limit(uid: int) -> bool:
    if uid == OWNER_ID:
        return False
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT last_ts FROM ratelimit WHERE user_id=?", (uid,))
    row = cur.fetchone()
    now = int(time.time())
    if not row:
        cur.execute("INSERT INTO ratelimit(user_id, last_ts) VALUES(?,?)", (uid, now))
        con.commit()
        con.close()
        return False
    last = row[0] or 0
    if (now - last) < RATE_LIMIT_S:
        con.close()
        return True
    cur.execute("UPDATE ratelimit SET last_ts=? WHERE user_id=?", (now, uid))
    con.commit()
    con.close()
    return False

# ====== Small utils ======
def fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.2f} {u}"
        size /= 1024

def safe_trunc(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

def is_terabox_url(url: str) -> bool:
    patterns = [
        r"terabox\.com", r"teraboxapp\.com", r"terabox\.app",
        r"nephobox\.com", r"mirrobox\.com", r"freeterabox\.com",
        r"tibibox\.com", r"teraboxlink\.com",
    ]
    return any(re.search(p, url) for p in patterns)

def extract_short_code(url: str) -> Optional[str]:
    # Supports /s/<code> and ?surl=<code>
    try:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        if "/s/" in u.path:
            parts = u.path.split("/")
            idx = parts.index("s")
            code = parts[idx + 1] if idx + 1 < len(parts) else None
            if code:
                return code
        qs = parse_qs(u.query or "")
        if "surl" in qs and qs["surl"]:
            return qs["surl"][0]
    except Exception:
        pass
    return None

def find_between(hay: str, left: str, right: str) -> Optional[str]:
    try:
        i = hay.index(left) + len(left)
        j = hay.index(right, i)
        return hay[i:j]
    except ValueError:
        return None

# ====== TeraBox resolver ======
@dataclass
class ResolveResult:
    ok: bool
    error: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    dlink: Optional[str] = None
    direct_link: Optional[str] = None
    thumb: Optional[str] = None
    source: Optional[str] = None

async def resolve_terabox(url: str) -> ResolveResult:
    """
    Re-implementation of a common approach used in many repos:
    1) Request share page to fetch jsToken + dp-logid + shorturl
    2) Call /share/list to get the file dlink
    3) HEAD the dlink to get its final redirect (direct CDN link)
    """
    # Normalize some mirror domains to terabox.app/terabox.com
    url = url.replace("teraboxlink.com", "terabox.com")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=HEADERS) as client:
        try:
            r = await client.get(url)
        except Exception as e:
            return ResolveResult(ok=False, error=f"Fetch failed: {e!s}")

        if r.status_code != 200:
            return ResolveResult(ok=False, error=f"HTTP {r.status_code} on initial URL")

        html = r.text
        default_thumb = find_between(html, 'og:image" content="', '"') or None
        logid = find_between(html, "dp-logid=", "&")
        jsToken = find_between(html, "fn%28%22", "%22%29")

        # Derive short code from final URL (after redirects) or original
        short = extract_short_code(str(r.url)) or extract_short_code(url)
        if not (short and logid and jsToken):
            return ResolveResult(ok=False, error="Missing required parameters (short/logid/jsToken) — link may be private.")

        share_list = (
            f"https://www.terabox.app/share/list?app_id=250528&web=1&channel=0"
            f"&jsToken={jsToken}&dp-logid={logid}&page=1&num=20&by=name&order=asc"
            f"&shorturl={short}&root=1"
        )
        try:
            j = await client.get(share_list, headers=HEADERS)
            data = j.json()
        except Exception as e:
            return ResolveResult(ok=False, error=f"Failed loading share/list: {e!s}")

        if data.get("errno"):
            return ResolveResult(ok=False, error=f"TeraBox API error: errno={data['errno']}")

        items = data.get("list") or []
        if not items:
            return ResolveResult(ok=False, error="No files found in this share.")

        file0 = items[0]
        dlink = file0.get("dlink")
        fname = file0.get("server_filename") or "file"
        fsize = int(file0.get("size") or 0)
        thumb = (file0.get("thumbs") or {}).get("url3", default_thumb)

        if not dlink:
            return ResolveResult(ok=False, error="No downloadable link exposed (private or unsupported).")

        # Get the final redirect (direct CDN URL) via a HEAD
        try:
            head = await client.head(dlink, headers=HEADERS, allow_redirects=False)
            direct = head.headers.get("location") or dlink
        except Exception as e:
            direct = dlink  # fall back

        return ResolveResult(
            ok=True,
            file_name=fname,
            file_size=fsize,
            dlink=dlink,
            direct_link=direct,
            thumb=thumb,
            source="share/list",
        )

# ====== Bot UI ======
router = Router()

def main_menu(uid: int) -> InlineKeyboardMarkup:
    auto_short, auto_mirror = db_get_settings(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Paste TeraBox Link", callback_data="example")
    kb.button(text=("🗜️ Auto‑Short: ON" if auto_short else "🗜️ Auto‑Short: OFF"), callback_data="toggle_short")
    kb.button(text=("📥 Auto‑Mirror: ON" if auto_mirror else "📥 Auto‑Mirror: OFF"), callback_data="toggle_mirror")
    kb.button(text="📚 Batch Mode", callback_data="batch_help")
    kb.button(text="ℹ️ How to Make Public", callback_data="guide_public")
    kb.button(text="⚙️ Settings", callback_data="settings")
    kb.button(text="🆘 Help", callback_data="help")
    if uid == OWNER_ID:
        kb.button(text="🛠 Admin", callback_data="admin")
    kb.adjust(2,2,2,2)
    return kb.as_markup()

def back_menu(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Back", callback_data="back")
    return kb.as_markup()

@router.message(CommandStart())
async def on_start(m: Message, bot: Bot):
    db_upsert_user(m)
    await m.answer(
        "👋 <b>Welcome!</b>\n"
        "Send me a <u>TeraBox</u> link and I'll try to give you a direct download URL.\n\n"
        "• Supports: terabox.com / terabox.app / teraboxapp.com (and common mirrors)\n"
        "• If a link is private or requires login, I'll tell you what to change.\n\n"
        "Tip: You can send multiple links separated by new lines.",
        reply_markup=main_menu(m.from_user.id),
        parse_mode=ParseMode.HTML
    )

@router.message(Command("ping"))
async def on_ping(m: Message):
    await m.reply("🏓 Pong.")

@router.message(Command("id"))
async def on_id(m: Message):
    await m.reply(f"👤 Your ID: <code>{m.from_user.id}</code>", parse_mode=ParseMode.HTML)

@router.message(Command("help"))
async def on_help_cmd(m: Message):
    await m.answer(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id))

HELP_TEXT = (
    "<b>How it works</b>\n"
    "1) Send a TeraBox share link.\n"
    "2) I'll fetch the file list and extract the direct CDN URL.\n"
    "3) If enabled, I can also shorten the URL and/or mirror small files to Telegram.\n\n"
    "<b>Batch mode</b>\n"
    "Send multiple links separated by spaces or new lines. I'll process them one by one.\n\n"
    "<b>Limits</b>\n"
    f"• Cooldown: <u>{RATE_LIMIT_S}s per user</u> (owner exempt)\n"
    f"• Mirror limit: <u>{fmt_bytes(MIRROR_MAX_BYTES)}</u>\n"
)

GUIDE_PUBLIC = (
    "<b>Make Your TeraBox Link Public</b>\n"
    "• Ensure the share is set to <i>Public</i> (not private or password‑protected).\n"
    "• Use a standard share URL like: <code>https://www.terabox.com/s/XXXX</code>\n"
    "• Avoid requiring login; otherwise the direct link cannot be fetched.\n"
    "• If it's a folder with many files, I will use the first item.\n"
)

SETTINGS_TEXT = (
    "<b>Settings</b>\n"
    "• Auto‑Short: If ON, I’ll attempt to shorten links (via tinyurl).\n"
    "• Auto‑Mirror: If ON and file ≤ 2GB, I’ll try uploading the file to Telegram.\n"
)

BATCH_TEXT = (
    "<b>Batch Mode</b>\n"
    "Send multiple links separated by spaces or new lines. Example:\n\n"
    "https://www.terabox.com/s/abcd1234\nhttps://teraboxapp.com/s/efgh5678\n"
)

# ====== Callbacks ======
@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=main_menu(c.from_user.id))
    await c.answer()

@router.callback_query(F.data == "example")
async def cb_example(c: CallbackQuery):
    await c.message.answer("Paste a TeraBox link (or multiple).")
    await c.answer()

@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_menu(c.from_user.id))
    await c.answer()

@router.callback_query(F.data == "guide_public")
async def cb_public(c: CallbackQuery):
    await c.message.edit_text(GUIDE_PUBLIC, parse_mode=ParseMode.HTML, reply_markup=back_menu(c.from_user.id))
    await c.answer()

@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    auto_short, auto_mirror = db_get_settings(c.from_user.id)
    txt = SETTINGS_TEXT + f"\n\nAuto‑Short: <b>{'ON' if auto_short else 'OFF'}</b>\nAuto‑Mirror: <b>{'ON' if auto_mirror else 'OFF'}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text=("🗜️ Toggle Auto‑Short" ), callback_data="toggle_short")
    kb.button(text=("📥 Toggle Auto‑Mirror"), callback_data="toggle_mirror")
    kb.button(text="⬅️ Back", callback_data="back")
    await c.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data == "toggle_short")
async def cb_tshort(c: CallbackQuery):
    newv = db_toggle(c.from_user.id, "auto_short")
    await c.answer(f"Auto‑Short is now {'ON' if newv else 'OFF'}.", show_alert=False)
    # Refresh menu
    await c.message.edit_reply_markup(reply_markup=main_menu(c.from_user.id))

@router.callback_query(F.data == "toggle_mirror")
async def cb_tmirror(c: CallbackQuery):
    newv = db_toggle(c.from_user.id, "auto_mirror")
    await c.answer(f"Auto‑Mirror is now {'ON' if newv else 'OFF'}.", show_alert=False)
    await c.message.edit_reply_markup(reply_markup=main_menu(c.from_user.id))

@router.callback_query(F.data == "batch_help")
async def cb_batch(c: CallbackQuery):
    await c.message.edit_text(BATCH_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_menu(c.from_user.id))
    await c.answer()

# ====== Admin (Owner only) ======
def owner_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 Broadcast", callback_data="owner_bcast")
    kb.button(text="📊 Stats", callback_data="owner_stats")
    kb.button(text="⬅️ Back", callback_data="back")
    kb.adjust(2,1)
    return kb.as_markup()

@router.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed.", show_alert=True)
        return
    await c.message.edit_text("Owner panel:", reply_markup=owner_kb())

@router.callback_query(F.data == "owner_stats")
async def cb_owner_stats(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Nope.", show_alert=True)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    con.close()
    await c.answer(f"Users: {total}", show_alert=True)

@router.callback_query(F.data == "owner_bcast")
async def cb_owner_bcast(c: CallbackQuery, bot: Bot):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Nope.", show_alert=True)
    await c.message.answer("Send the broadcast text (HTML supported).")
    # Set a simple state flag in DB ratelimit table (hacky but fine for single-file)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("UPDATE ratelimit SET last_ts = -1 WHERE user_id=?", (OWNER_ID,))
    con.commit(); con.close()
    await c.answer()

# Capture next owner message as broadcast
@router.message(F.from_user.id == OWNER_ID)
async def on_owner_msg(m: Message, bot: Bot):
    # Check if waiting for broadcast (our hacky flag: last_ts == -1)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT last_ts FROM ratelimit WHERE user_id=?", (OWNER_ID,))
    row = cur.fetchone()
    if row and row[0] == -1:
        # Reset flag
        cur.execute("UPDATE ratelimit SET last_ts=? WHERE user_id=?", (int(time.time()), OWNER_ID))
        con.commit(); con.close()

        # Broadcast
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT user_id FROM users")
        all_ids = [r[0] for r in cur.fetchall()]
        con.close()
        sent = 0
        for uid in all_ids:
            try:
                await bot.send_message(uid, m.html_text, parse_mode=ParseMode.HTML)
                sent += 1
                await asyncio.sleep(0.03)
            except Exception:
                pass
        return await m.reply(f"📣 Broadcast sent to {sent} users.")
    # Else: fall through to normal message handlers below

# ====== Core: handle links & batches ======
@router.message(F.text.func(lambda s: s and ("http://" in s or "https://" in s)))
async def on_links(m: Message, bot: Bot):
    db_upsert_user(m)
    text = m.text.strip()

    # Rate limit (owner exempt)
    if db_should_limit(m.from_user.id):
        return await m.reply(f"⏳ Slow down. Try again in {RATE_LIMIT_S}s.")

    # Extract all URLs
    urls = re.findall(r"https?://\S+", text)
    if not urls:
        return await m.reply("Send a TeraBox link.")

    results = []
    for idx, url in enumerate(urls, start=1):
        if not is_terabox_url(url):
            results.append(("❌", url, "Not a TeraBox link."))
            continue

        msg = await m.reply(f"🔎 Resolving {idx}/{len(urls)}…")
        res = await resolve_terabox(url)
        if not res.ok:
            await msg.edit_text(f"❌ <b>Failed:</b> {safe_trunc(url)}\n<code>{res.error}</code>",
                                parse_mode=ParseMode.HTML)
            results.append(("❌", url, res.error or "Unknown error"))
            continue

        # Optional: short link
        auto_short, auto_mirror = db_get_settings(m.from_user.id)
        final_link = res.direct_link or res.dlink or ""
        short_link = None
        if auto_short and final_link:
            short_link = await try_shortlink(final_link)

        # Build inline buttons
        kb = InlineKeyboardBuilder()
        if final_link:
            kb.button(text="🔓 Open Direct Link", url=final_link)
        if res.dlink and res.dlink != final_link:
            kb.button(text="↗️ Source DLink", url=res.dlink)
        if short_link:
            kb.button(text="🗜️ Short Link", url=short_link)
        kb.button(text="ℹ️ Info", callback_data=f"info:{idx}")
        if auto_mirror and (res.file_size or 0) <= MIRROR_MAX_BYTES:
            kb.button(text="📥 Mirror to Telegram", callback_data=f"mirror:{idx}")
        kb.adjust(1,2)

        caption = (
            f"✅ <b>Direct Link Ready</b>\n"
            f"• File: <code>{safe_trunc(res.file_name or 'file', 80)}</code>\n"
            f"• Size: <code>{fmt_bytes(res.file_size or 0)}</code>\n"
            f"• Source: <code>{res.source}</code>"
        )
        await msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())

        # Save in an in-memory store (attach to message object via bot data) for callbacks
        key = f"{m.chat.id}:{m.message_id}:{idx}"
        bot_data_store[key] = res
        results.append(("✅", url, "OK"))

    # Summarize batch (optional)
    if len(urls) > 1:
        ok = sum(1 for x in results if x[0] == "✅")
        fail = len(results) - ok
        await m.answer(f"Batch done. ✅ {ok} | ❌ {fail}")

# In-memory store for per-message results (kept minimal; restarts will clear it)
bot_data_store: Dict[str, ResolveResult] = {}

async def try_shortlink(url: str) -> Optional[str]:
    # TinyURL simple API
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://tinyurl.com/api-create.php", params={"url": url})
            if r.status_code == 200 and r.text.startswith("http"):
                return r.text.strip()
    except Exception:
        pass
    return None

# Callback for info/mirror referencing prior results
@router.callback_query(F.data.startswith("info:"))
async def cb_info(c: CallbackQuery):
    idx = c.data.split(":", 1)[1]
    key = f"{c.message.chat.id}:{c.message.reply_to_message.message_id if c.message.reply_to_message else c.message.message_id}:{idx}"
    res = bot_data_store.get(key)
    if not res:
        return await c.answer("Context expired. Send link again.", show_alert=True)
    txt = (
        f"<b>File Info</b>\n"
        f"Name: <code>{res.file_name}</code>\n"
        f"Size: <code>{fmt_bytes(res.file_size or 0)}</code>\n"
        f"DLink host: <code>{re.sub(r'^https?://', '', res.dlink or '')}</code>\n"
        f"Direct host: <code>{re.sub(r'^https?://', '', res.direct_link or '')}</code>"
    )
    await c.message.answer(txt, parse_mode=ParseMode.HTML)
    await c.answer()

@router.callback_query(F.data.startswith("mirror:"))
async def cb_mirror(c: CallbackQuery, bot: Bot):
    idx = c.data.split(":", 1)[1]
    key = f"{c.message.chat.id}:{c.message.reply_to_message.message_id if c.message.reply_to_message else c.message.message_id}:{idx}"
    res = bot_data_store.get(key)
    if not res or not res.direct_link:
        return await c.answer("Nothing to mirror.", show_alert=True)

    if (res.file_size or 0) > MIRROR_MAX_BYTES:
        return await c.answer(f"Too large (> {fmt_bytes(MIRROR_MAX_BYTES)}).", show_alert=True)

    msg = await c.message.answer("⬇️ Downloading to server…")
    # Stream download to a temp file
    fname = re.sub(r"[^\w\.\- ]+", "_", res.file_name or "file.bin")
    tmp = f"dl_{int(time.time())}_{fname}"
    try:
        total = res.file_size or 0
        done = 0
        last_edit = 0

        async with httpx.AsyncClient(timeout=None, headers={"User-Agent": UA}) as cl:
            async with cl.stream("GET", res.direct_link) as r:
                if r.status_code >= 400:
                    return await msg.edit_text(f"❌ HTTP {r.status_code} while downloading.")
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024*512):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if total and now - last_edit > 1.5:
                            pct = (done/total)*100
                            await msg.edit_text(f"⬇️ {fmt_bytes(done)} / {fmt_bytes(total)} ({pct:.1f}%)")
                            last_edit = now

        # Upload to Telegram
        await msg.edit_text("📤 Uploading to Telegram…")
        try:
            await bot.send_document(c.message.chat.id, FSInputFile(tmp, filename=fname), caption=f"Mirrored: {safe_trunc(fname, 64)}")
        except Exception as e:
            await msg.edit_text(f"❌ Upload failed: {e!s}")
            return
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Mirror failed: {e!s}")
    finally:
        with contextlib.suppress(Exception):
            os.remove(tmp)

# ====== Fallback text handler ======
@router.message()
async def on_any(m: Message):
    db_upsert_user(m)
    await m.reply(
        "Send a <b>TeraBox</b> link to get a direct download URL, or tap the menu below.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(m.from_user.id)
    )

# ====== Startup ======
async def _main():
    db_init()
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot started. Press Ctrl+C to stop.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        import uvloop, asyncio as _a
        _a.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        pass
    asyncio.run(_main())
