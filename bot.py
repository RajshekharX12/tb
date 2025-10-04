#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeraBox Direct-Link Bot (aiogram v3, single-file) — Patched
===========================================================
- Fix: aiogram 3.7 Bot(...) default parse_mode via DefaultBotProperties
- Fix: robust URL detection (text / caption / entities)
- Fix: callback context key stored against the edited status message
- UI: Inline menus re-grouped (Extras, Back), tidy button layout
- Feature: 👮 Police (system reviewer) — brief VPS specs
- Safety: disk-space guard before mirror
"""
import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import time
import platform
import shutil
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from importlib import metadata

# ====== Configuration ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("ERROR: Set BOT_TOKEN in .env first.")

OWNER_ID = 7940894807  # owner bypass & admin panel

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("terabox-bot")

# SQLite for tiny state
DB_PATH = "bot_data.sqlite3"
RATE_LIMIT_S = 10
MIRROR_MAX_BYTES = 2_000_000_000  # ~1.86 GiB

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

# ====== DB helpers ======
def db_init() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT, last_name TEXT, username TEXT, first_seen INTEGER
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
    con.commit(); con.close()

def db_upsert_user(m: Message) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users(user_id, first_name, last_name, username, first_seen)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username
    """, (m.from_user.id, m.from_user.first_name or "", m.from_user.last_name or "", m.from_user.username or "", int(time.time())))
    cur.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (m.from_user.id,))
    con.commit(); con.close()

def db_get_settings(uid: int) -> Tuple[bool, bool]:
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT auto_short, auto_mirror FROM settings WHERE user_id=?", (uid,))
    row = cur.fetchone(); con.close()
    if not row: return False, False
    return bool(row[0]), bool(row[1])

def db_toggle(uid: int, field: str) -> bool:
    assert field in ("auto_short", "auto_mirror")
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute(f"SELECT {field} FROM settings WHERE user_id=?", (uid,))
    row = cur.fetchone()
    val = 0 if (row and row[0]) else 1
    cur.execute(f"UPDATE settings SET {field}=? WHERE user_id=?", (val, uid))
    con.commit(); con.close()
    return bool(val)

def db_should_limit(uid: int) -> bool:
    if uid == OWNER_ID: return False
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT last_ts FROM ratelimit WHERE user_id=?", (uid,))
    row = cur.fetchone()
    now = int(time.time())
    if not row:
        cur.execute("INSERT INTO ratelimit(user_id, last_ts) VALUES(?,?)", (uid, now))
        con.commit(); con.close()
        return False
    last = row[0] or 0
    if (now - last) < RATE_LIMIT_S:
        con.close(); return True
    cur.execute("UPDATE ratelimit SET last_ts=? WHERE user_id=?", (now, uid))
    con.commit(); con.close()
    return False

# ====== Utils ======
def fmt_bytes(n: int) -> str:
    units = ["B","KB","MB","GB","TB"]; size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]: return f"{size:.2f} {u}"
        size /= 1024

def safe_trunc(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

def is_terabox_url(url: str) -> bool:
    patterns = [r"terabox\.com", r"teraboxapp\.com", r"terabox\.app",
                r"nephobox\.com", r"mirrobox\.com", r"freeterabox\.com",
                r"tibibox\.com", r"teraboxlink\.com"]
    return any(re.search(p, url) for p in patterns)

def extract_short_code(url: str) -> Optional[str]:
    try:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        if "/s/" in u.path:
            parts = u.path.split("/")
            idx = parts.index("s")
            if idx + 1 < len(parts): return parts[idx + 1]
        qs = parse_qs(u.query or "")
        if "surl" in qs and qs["surl"]: return qs["surl"][0]
    except Exception:
        pass
    return None

def find_between(hay: str, left: str, right: str) -> Optional[str]:
    try:
        i = hay.index(left) + len(left); j = hay.index(right, i); return hay[i:j]
    except ValueError:
        return None

# ====== Resolver ======
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

        try:
            head = await client.head(dlink, headers=HEADERS, allow_redirects=False)
            direct = head.headers.get("location") or dlink
        except Exception:
            direct = dlink

        return ResolveResult(ok=True, file_name=fname, file_size=fsize, dlink=dlink, direct_link=direct, thumb=thumb, source="share/list")

# ====== Bot UI ======
router = Router()

def main_menu(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Paste Link", callback_data="example")
    kb.button(text="📚 Batch Mode", callback_data="batch_help")
    kb.button(text="⚙️ Settings", callback_data="settings")
    kb.button(text="✨ Extras", callback_data="extras")
    kb.button(text="🆘 Help", callback_data="help")
    if uid == OWNER_ID: kb.button(text="🛠 Admin", callback_data="admin")
    kb.adjust(2,2,1,1)
    return kb.as_markup()

def back_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder(); kb.button(text="⬅️ Back", callback_data="back"); return kb.as_markup()

@router.message(CommandStart())
async def on_start(m: Message, bot: Bot):
    db_upsert_user(m)
    await m.answer(
        "👋 <b>Welcome!</b>\n"
        "Send a <u>TeraBox</u> link and I'll fetch a direct download URL.\n\n"
        "• Works with terabox.com / terabox.app / teraboxapp.com and mirrors\n"
        "• Private/passworded shares won’t resolve\n\n"
        "Tip: You can send multiple links separated by new lines.",
        reply_markup=main_menu(m.from_user.id),
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def on_help_cmd(m: Message):
    await m.answer(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id))

HELP_TEXT = (
    "<b>How it works</b>\n"
    "1) Send a TeraBox share link.\n"
    "2) I fetch the file list and extract a direct CDN URL.\n"
    "3) Optional: shorten URL and/or mirror small files to Telegram.\n\n"
    "<b>Batch</b>: send multiple links separated by spaces or new lines.\n"
    f"<b>Limit</b>: cooldown <u>{RATE_LIMIT_S}s</u> per user; mirror ≤ <u>{fmt_bytes(MIRROR_MAX_BYTES)}</u>.\n"
)
GUIDE_PUBLIC = (
    "<b>Make Public</b>\n"
    "• Ensure share visibility is <i>Public</i>\n"
    "• Use URL like <code>https://www.terabox.com/s/XXXX</code>\n"
    "• If it’s a folder, I use the first file."
)
SETTINGS_TEXT = (
    "<b>Settings</b>\n"
    "• Auto‑Short: shorten direct links (TinyURL)\n"
    "• Auto‑Mirror: if ON and size ≤ limit, upload to Telegram"
)
BATCH_TEXT = (
    "<b>Batch Mode</b>\n"
    "Send multiple links separated by spaces/new lines, e.g.:\n"
    "https://www.terabox.com/s/abcd1234\\nhttps://teraboxapp.com/s/efgh5678"
)

@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.message.edit_text("Main menu:", reply_markup=main_menu(c.from_user.id))

@router.callback_query(F.data == "example")
async def cb_example(c: CallbackQuery):
    await c.message.answer("Paste a TeraBox link (or multiple)."); await c.answer()

@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_menu())

@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    auto_short, auto_mirror = db_get_settings(c.from_user.id)
    txt = SETTINGS_TEXT + f"\\n\\nAuto‑Short: <b>{'ON' if auto_short else 'OFF'}</b>\\nAuto‑Mirror: <b>{'ON' if auto_mirror else 'OFF'}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🗜️ Toggle Auto‑Short", callback_data="toggle_short")
    kb.button(text="📥 Toggle Auto‑Mirror", callback_data="toggle_mirror")
    kb.button(text="⬅️ Back", callback_data="back")
    await c.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())

@router.callback_query(F.data == "toggle_short")
async def cb_tshort(c: CallbackQuery):
    newv = db_toggle(c.from_user.id, "auto_short")
    await c.answer(f"Auto‑Short is now {'ON' if newv else 'OFF'}.")
    await c.message.edit_reply_markup(reply_markup=main_menu(c.from_user.id))

@router.callback_query(F.data == "toggle_mirror")
async def cb_tmirror(c: CallbackQuery):
    newv = db_toggle(c.from_user.id, "auto_mirror")
    await c.answer(f"Auto‑Mirror is now {'ON' if newv else 'OFF'}.")
    await c.message.edit_reply_markup(reply_markup=main_menu(c.from_user.id))

@router.callback_query(F.data == "batch_help")
async def cb_batch(c: CallbackQuery):
    await c.message.edit_text(BATCH_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_menu())

# ====== Extras (includes 👮 Police) ======
def extras_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👮 Police (System Review)", callback_data="extras_police")
    kb.button(text="ℹ️ About", callback_data="extras_about")
    kb.button(text="⬅️ Back", callback_data="back")
    kb.adjust(1,1,1)
    return kb.as_markup()

@router.callback_query(F.data == "extras")
async def cb_extras(c: CallbackQuery):
    await c.message.edit_text("Extras:", reply_markup=extras_menu())

@router.callback_query(F.data == "extras_about")
async def cb_extras_about(c: CallbackQuery):
    def ver(name):
        try: return metadata.version(name)
        except Exception: return "unknown"
    txt = (
        "<b>About</b>\\n"
        f"• aiogram: <code>{ver('aiogram')}</code>\\n"
        f"• httpx: <code>{ver('httpx')}</code>\\n"
        f"• uvloop: <code>{ver('uvloop')}</code>\\n"
        "Single‑file TeraBox direct‑link bot with inline UI."
    )
    await c.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=extras_menu())

@router.callback_query(F.data == "extras_police")
async def cb_extras_police(c: CallbackQuery):
    try:
        os_name = platform.system(); os_release = platform.release(); py = platform.python_version()
        cpu_count = os.cpu_count() or 1
        # CPU model (Linux)
        cpu_model = ""
        try:
            with open("/proc/cpuinfo","r") as f:
                for line in f:
                    if "model name" in line:
                        cpu_model = line.split(":",1)[1].strip(); break
        except Exception:
            cpu_model = platform.processor() or ""
        # RAM
        mem_total = mem_free = 0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"): mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"): mem_free = int(line.split()[1]) * 1024
        except Exception: pass
        # Disk
        total, used, free = shutil.disk_usage(".")
        # Uptime
        up_s = 0
        try:
            with open("/proc/uptime") as f:
                up_s = int(float(f.read().split()[0]))
        except Exception: pass
        def hms(sec:int):
            h=sec//3600; m=(sec%3600)//60; s=sec%60; return f"{h}h {m}m {s}s"
        # Lib versions
        def ver(name):
            try: return metadata.version(name)
            except Exception: return "unknown"
        txt = (
            "👮 <b>Police — System Review</b>\\n"
            f"• OS: <code>{os_name} {os_release}</code>\\n"
            f"• Python: <code>{py}</code>\\n"
            f"• CPU: <code>{cpu_count}x {cpu_model[:40]+'…' if len(cpu_model)>40 else cpu_model}</code>\\n"
            f"• RAM: <code>{fmt_bytes(mem_total)} total / {fmt_bytes(mem_free)} free</code>\\n"
            f"• Disk: <code>{fmt_bytes(total)} total / {fmt_bytes(free)} free</code>\\n"
            f"• Uptime: <code>{hms(up_s)}</code>\\n"
            f"• aiogram/httpx/uvloop: <code>{ver('aiogram')}</code>/<code>{ver('httpx')}</code>/<code>{ver('uvloop')}</code>"
        )
    except Exception as e:
        txt = f"👮 System check failed: <code>{e!s}</code>"
    await c.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=extras_menu())

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
    if c.from_user.id != OWNER_ID: return await c.answer("Not allowed.", show_alert=True)
    await c.message.edit_text("Owner panel:", reply_markup=owner_kb())

@router.callback_query(F.data == "owner_stats")
async def cb_owner_stats(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Nope.", show_alert=True)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
    con.close()
    await c.answer(f"Users: {total}", show_alert=True)

@router.callback_query(F.data == "owner_bcast")
async def cb_owner_bcast(c: CallbackQuery, bot: Bot):
    if c.from_user.id != OWNER_ID: return await c.answer("Nope.", show_alert=True)
    await c.message.answer("Send the broadcast text (HTML supported).")
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("UPDATE ratelimit SET last_ts = -1 WHERE user_id=?", (OWNER_ID,))
    con.commit(); con.close()
    await c.answer()

@router.message(F.from_user.id == OWNER_ID)
async def on_owner_msg(m: Message, bot: Bot):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT last_ts FROM ratelimit WHERE user_id=?", (OWNER_ID,))
    row = cur.fetchone()
    if row and row[0] == -1:
        cur.execute("UPDATE ratelimit SET last_ts=? WHERE user_id=?", (int(time.time()), OWNER_ID))
        con.commit(); con.close()
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT user_id FROM users"); ids = [r[0] for r in cur.fetchall()]
        con.close()
        sent = 0
        for uid in ids:
            with contextlib.suppress(Exception):
                await bot.send_message(uid, m.html_text, parse_mode=ParseMode.HTML)
                sent += 1; await asyncio.sleep(0.03)
        return await m.reply(f"📣 Broadcast sent to {sent} users.")

# ====== URL handlers ======
@router.message(F.text.regexp(r'https?://\S+'))
async def on_links_text(m: Message, bot: Bot):
    await process_links_message(m, bot, m.text or "")

@router.message(F.caption.regexp(r'https?://\S+'))
async def on_links_caption(m: Message, bot: Bot):
    await process_links_message(m, bot, m.caption or "")

@router.message(F.entities)
async def on_links_entities(m: Message, bot: Bot):
    if not m.entities: return
    text = m.text or m.caption or ""
    urls = []
    for ent in m.entities:
        if ent.type == "url":
            try: urls.append(ent.extract_from(text))
            except Exception: pass
    if urls: await process_links_message(m, bot, "\\n".join(urls))

async def process_links_message(m: Message, bot: Bot, text: str):
    db_upsert_user(m)
    if db_should_limit(m.from_user.id): return await m.reply(f"⏳ Slow down. Try again in {RATE_LIMIT_S}s.")
    urls = re.findall(r'https?://\S+', text)
    if not urls: return await m.reply("Send a TeraBox link.")

    results = []
    for idx, url in enumerate(urls, start=1):
        if not is_terabox_url(url):
            results.append(("❌", url, "Not a TeraBox link.")); continue
        status = await m.reply(f"🔎 Resolving {idx}/{len(urls)}…")

        res = await resolve_terabox(url)
        if not res.ok:
            await status.edit_text(f"❌ <b>Failed:</b> {safe_trunc(url)}\\n<code>{res.error}</code>", parse_mode=ParseMode.HTML)
            results.append(("❌", url, res.error or "Unknown error")); continue

        auto_short, auto_mirror = db_get_settings(m.from_user.id)
        final_link = res.direct_link or res.dlink or ""
        short_link = None
        if auto_short and final_link: short_link = await try_shortlink(final_link)

        kb = InlineKeyboardBuilder(); rows = []
        if final_link: kb.button(text="🔓 Open Direct Link", url=final_link); rows.append(1)
        row2 = 0
        if short_link: kb.button(text="🗜️ Short Link", url=short_link); row2 += 1
        if res.dlink and res.dlink != final_link: kb.button(text="↗️ Source DLink", url=res.dlink); row2 += 1
        if row2: rows.append(row2)
        kb.button(text="ℹ️ Info", callback_data=f"info:{idx}")
        if auto_mirror and (res.file_size or 0) <= MIRROR_MAX_BYTES:
            kb.button(text="📥 Mirror", callback_data=f"mirror:{idx}"); rows.append(2)
        else:
            rows.append(1)
        kb.adjust(*rows)

        caption = (
            f"✅ <b>Direct Link Ready</b>\\n"
            f"• File: <code>{safe_trunc(res.file_name or 'file', 80)}</code>\\n"
            f"• Size: <code>{fmt_bytes(res.file_size or 0)}</code>\\n"
            f"• Source: <code>{res.source}</code>"
        )
        await status.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())

        # Store using the *status* message id (fix for callbacks)
        store_key = f"{status.chat.id}:{status.message_id}:{idx}"
        bot_data_store[store_key] = res
        results.append(("✅", url, "OK"))

    if len(urls) > 1:
        ok = sum(1 for x in results if x[0] == "✅"); fail = len(results) - ok
        await m.answer(f"Batch done. ✅ {ok} | ❌ {fail}")

bot_data_store: Dict[str, ResolveResult] = {}

async def try_shortlink(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://tinyurl.com/api-create.php", params={"url": url})
            if r.status_code == 200 and r.text.startswith("http"): return r.text.strip()
    except Exception: pass
    return None

@router.callback_query(F.data.startswith("info:"))
async def cb_info(c: CallbackQuery):
    idx = c.data.split(":", 1)[1]
    key = f"{c.message.chat.id}:{c.message.message_id}:{idx}"
    res = bot_data_store.get(key)
    if not res: return await c.answer("Context expired. Send link again.", show_alert=True)
    txt = (
        f"<b>File Info</b>\\n"
        f"Name: <code>{res.file_name}</code>\\n"
        f"Size: <code>{fmt_bytes(res.file_size or 0)}</code>\\n"
        f"DLink host: <code>{re.sub(r'^https?://', '', res.dlink or '')}</code>\\n"
        f"Direct host: <code>{re.sub(r'^https?://', '', res.direct_link or '')}</code>"
    )
    await c.message.answer(txt, parse_mode=ParseMode.HTML); await c.answer()

@router.callback_query(F.data.startswith("mirror:"))
async def cb_mirror(c: CallbackQuery, bot: Bot):
    idx = c.data.split(":", 1)[1]
    key = f"{c.message.chat.id}:{c.message.message_id}:{idx}"
    res = bot_data_store.get(key)
    if not res or not res.direct_link: return await c.answer("Nothing to mirror.", show_alert=True)
    if (res.file_size or 0) > MIRROR_MAX_BYTES:
        return await c.answer(f"Too large (> {fmt_bytes(MIRROR_MAX_BYTES)}).", show_alert=True)
    total, used, free = shutil.disk_usage("."); need = (res.file_size or 0) + 500_000_000
    if free < need: return await c.answer("Not enough disk space on the server.", show_alert=True)

    msg = await c.message.answer("⬇️ Downloading to server…")
    fname = re.sub(r"[^\w\.\- ]+", "_", res.file_name or "file.bin"); tmp = f"dl_{int(time.time())}_{fname}"
    try:
        total_sz = res.file_size or 0; done=0; last=0
        async with httpx.AsyncClient(timeout=None, headers={"User-Agent": UA}) as cl:
            async with cl.stream("GET", res.direct_link) as r:
                if r.status_code >= 400: return await msg.edit_text(f"❌ HTTP {r.status_code} while downloading.")
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024*512):
                        if not chunk: continue
                        f.write(chunk); done += len(chunk)
                        now = time.time()
                        if total_sz and now - last > 1.5:
                            await msg.edit_text(f"⬇️ {fmt_bytes(done)} / {fmt_bytes(total_sz)} ({(done/total_sz)*100:.1f}%)"); last = now
        await msg.edit_text("📤 Uploading to Telegram…")
        try:
            await bot.send_document(c.message.chat.id, FSInputFile(tmp, filename=fname), caption=f"Mirrored: {safe_trunc(fname, 64)}")
        except Exception as e:
            return await msg.edit_text(f"❌ Upload failed: {e!s}")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Mirror failed: {e!s}")
    finally:
        with contextlib.suppress(Exception): os.remove(tmp)

@router.message()
async def on_any(m: Message):
    db_upsert_user(m)
    await m.reply("Send a <b>TeraBox</b> link to get a direct download URL, or open the menu.", parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id))

# ====== Startup ======
async def _main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(); dp.include_router(router)
    log.info("Bot started. Press Ctrl+C to stop.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        import uvloop, asyncio as _a; _a.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        pass
    asyncio.run(_main())
