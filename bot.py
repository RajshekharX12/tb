#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeraBox Downloader Bot — aiogram v3 (single file)
================================================
• Single file: only .env with BOT_TOKEN is needed (optional TERABOX_COOKIE supported but not required)
• Paste a TeraBox share link → bot shows file info + Direct Link button + Download (≤ 500 MB)
• Inline UI: Help, Police (status), Back
• Built to avoid classic aiogram v3 pitfalls (parse_mode, init, polling args, message edits)

If TeraBox changes internals or the share is private, direct link may fail. Optional:
Add TERABOX_COOKIE=... in .env to improve success for some private shares.
"""

import asyncio
import os
import re
import time
import json
import math
import platform
import shutil
import socket
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse, parse_qs

# ---------- minimal .env loader (no external deps) ----------
def load_env():
    try:
        if os.path.exists(".env"):
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        os.environ.setdefault(k, v)
    except Exception:
        # ignore .env issues silently
        pass

load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TERABOX_COOKIE = os.environ.get("TERABOX_COOKIE", "").strip()  # optional

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN. Put it in .env as BOT_TOKEN=XXXX or export it.")

# ---------- HTTP client: prefer requests if available; else stdlib ----------
try:
    import requests  # type: ignore
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False
    import urllib.request
    import urllib.error

    class _Resp:
        def __init__(self, code, body, headers, url):
            self.status_code = code
            self.content = body
            self.headers = headers
            self.url = url
            try:
                self.text = body.decode("utf-8", "replace")
            except Exception:
                self.text = ""

        def json(self):
            return json.loads(self.text)

    class SimpleRequests:
        def request(self, method, url, headers=None, data=None, params=None):
            if params:
                from urllib.parse import urlencode
                url = url + ("&" if ("?" in url) else "?") + urlencode(params, doseq=True)
            req = urllib.request.Request(url, method=method.upper(), headers=headers or {})
            if data is not None:
                if isinstance(data, dict):
                    data = json.dumps(data).encode("utf-8")
                    req.add_header("Content-Type", "application/json")
                elif isinstance(data, str):
                    data = data.encode("utf-8")
            try:
                with urllib.request.urlopen(req, data=data) as r:
                    body = r.read()
                    headers = dict(r.headers.items())
                    return _Resp(r.getcode(), body, headers, r.geturl())
            except urllib.error.HTTPError as e:
                body = e.read()
                headers = dict(e.headers.items()) if e.headers else {}
                return _Resp(e.code, body, headers, url)
            except Exception as e:
                return _Resp(599, str(e).encode(), {}, url)

        def get(self, url, headers=None, params=None):
            return self.request("GET", url, headers=headers, params=params)

        def head(self, url, headers=None):
            # emulate HEAD using Range
            hdrs = dict(headers or {})
            hdrs["Range"] = "bytes=0-0"
            return self.request("GET", url, headers=hdrs)

        def post(self, url, headers=None, data=None, params=None):
            return self.request("POST", url, headers=headers, data=data, params=params)

    requests = SimpleRequests()  # type: ignore

# ---------- aiogram v3 ----------
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)

# ---------- utilities ----------
SUPPORTED_DOMAINS = [
    r"terabox\.com", r"teraboxapp\.com", r"nephobox\.com", r"mirrobox\.com",
    r"freeterabox\.com", r"1024tera\.com", r"4funbox\.com", r"momerybox\.com",
    r"terabox\.app",
]
URL_PATTERN = re.compile(r"(https?://\S+)", re.IGNORECASE)

def is_terabox_url(url: str) -> bool:
    return any(re.search(d, url, re.IGNORECASE) for d in SUPPORTED_DOMAINS)

def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_PATTERN.findall(text)
    if not m:
        return None
    for u in m:
        u2 = u.strip(").,>\"\n\r\t")
        if is_terabox_url(u2):
            return u2
    return None

def extract_surl(url: str) -> Optional[str]:
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        if "surl" in q and len(q["surl"]) > 0:
            return q["surl"][0]
        # /s/<code>
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "s":
            return parts[1]
        return None
    except Exception:
        return None

def human_size(num: int) -> str:
    if num <= 0:
        return "0 B"
    units = ["B","KB","MB","GB","TB","PB"]
    i = min(int(math.floor(math.log(num, 1024))), len(units)-1)
    s = round(num / (1024 ** i), 2)
    return f"{s} {units[i]}"

def uptime_str() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days = int(secs // 86400); secs -= days*86400
        hours = int(secs // 3600); secs -= hours*3600
        mins = int(secs // 60)
        return f"{days}d {hours}h {mins}m"
    except Exception:
        return "-"

def mem_info() -> Tuple[str,str]:
    try:
        with open("/proc/meminfo") as f:
            mem = f.read()
        def _val(key):
            m = re.search(rf"^{key}:\s+(\d+)\s+kB", mem, re.M)
            return int(m.group(1))*1024 if m else 0
        total = _val("MemTotal")
        avail = _val("MemAvailable")
        used = total - avail
        return human_size(used), human_size(total)
    except Exception:
        return "-", "-"

def disk_info() -> Tuple[str,str]:
    try:
        st = shutil.disk_usage("/")
        return human_size(st.used), human_size(st.total)
    except Exception:
        return "-", "-"

# ---------- TeraBox client ----------
HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
}

def _headers(extra: Dict[str,str] = None) -> Dict[str,str]:
    h = dict(HEADERS_BASE)
    if TERABOX_COOKIE:
        h["Cookie"] = TERABOX_COOKIE
    if extra:
        h.update(extra)
    return h

@dataclass
class TeraMeta:
    file_name: str
    size_bytes: int
    size_h: str
    thumb: Optional[str]
    dlink: Optional[str]        # pre-redirect link from API
    direct_link: Optional[str]  # resolved final URL

class TeraBox:
    API_HOST = "https://www.terabox.com"

    @staticmethod
    def _req(method: str, url: str, **kwargs):
        if method == "GET":
            return requests.get(url, **kwargs)
        elif method == "POST":
            return requests.post(url, **kwargs)
        else:
            return requests.request(method, url, **kwargs)  # pragma: no cover

    @classmethod
    def fetch_page(cls, any_url: str):
        return cls._req("GET", any_url, headers=_headers())

    @classmethod
    def share_list(cls, surl: str) -> Dict[str, Any]:
        url = f"{cls.API_HOST}/share/list"
        params = {
            "app_id": "250528",
            "web": "1",
            "root": "1",
            "num": "100",
            "page": "1",
            "shorturl": surl
        }
        r = cls._req("GET", url, headers=_headers(), params=params)
        try:
            return r.json()
        except Exception:
            return {}

    @classmethod
    def sharedownload(cls, surl: str, sign: Optional[str], ts: Optional[str], fs_id: int) -> Dict[str, Any]:
        if not (sign and ts and fs_id):
            return {}
        base_params = {
            "app_id": "250528",
            "web": "1",
            "channel": "share",
            "sign": sign,
            "timestamp": ts,
            "surl": surl
        }
        # try fid_list
        p1 = dict(base_params); p1["fid_list"] = f"[{fs_id}]"
        j = cls._req("GET", f"{cls.API_HOST}/api/sharedownload", headers=_headers(), params=p1).json()
        try:
            if (j.get("list") or [{}])[0].get("dlink"):
                return j
        except Exception:
            pass
        # fallback fidlist
        p2 = dict(base_params); p2["fidlist"] = f"[{fs_id}]"
        try:
            return cls._req("GET", f"{cls.API_HOST}/api/sharedownload", headers=_headers(), params=p2).json()
        except Exception:
            return {}

    @staticmethod
    def parse_sign_ts(page_html: str) -> Tuple[Optional[str], Optional[str]]:
        sign = ts = None
        for p in [
            r'sign["\']?\s*[:=]\s*["\']([a-zA-Z0-9]+)["\']',
            r'window\.sign\s*=\s*["\']([a-zA-Z0-9]+)["\']',
        ]:
            m = re.search(p, page_html)
            if m:
                sign = m.group(1)
                break
        for p in [
            r'timestamp["\']?\s*[:=]\s*([0-9]{10,})',
            r'window\.timestamp\s*=\s*([0-9]{10,})',
        ]:
            m = re.search(p, page_html)
            if m:
                ts = m.group(1)
                break
        return sign, ts

    @classmethod
    def resolve_direct(cls, dlink: str) -> Optional[str]:
        try:
            r = requests.head(dlink, headers=_headers())
            loc = None
            # requests (real) returns dict-like headers; SimpleRequests does too
            if hasattr(r, "headers") and isinstance(r.headers, dict):
                loc = r.headers.get("Location") or r.headers.get("location")
            return loc or dlink
        except Exception:
            return dlink

    @classmethod
    def fetch_meta(cls, any_url: str) -> Tuple[Optional[TeraMeta], Optional[str]]:
        if not is_terabox_url(any_url):
            return None, "Not a supported TeraBox URL."

        surl = extract_surl(any_url)
        if not surl:
            return None, "Could not parse share code (surl) from the URL."

        pg = cls.fetch_page(any_url)
        if getattr(pg, "status_code", 0) != 200 or not getattr(pg, "text", ""):
            return None, "Failed to open the share page."

        sign, ts = cls.parse_sign_ts(pg.text)

        listing = cls.share_list(surl)
        if listing.get("errno", 0) != 0 or not listing.get("list"):
            return None, "API returned no files. Link may be private or expired."

        file_info = listing["list"][0]
        fs_id = int(file_info.get("fs_id", 0))
        size_b = int(file_info.get("size", 0))
        name = file_info.get("server_filename") or file_info.get("filename") or "file"
        thumb = (file_info.get("thumbs") or {}).get("url3")

        dlink = None
        resolved = None
        dl = cls.sharedownload(surl, sign, ts, fs_id)
        try:
            dl_list = dl.get("list") or []
            if dl_list:
                dlink = dl_list[0].get("dlink")
                if dlink:
                    resolved = cls.resolve_direct(dlink)
        except Exception:
            pass

        meta = TeraMeta(
            file_name=name,
            size_bytes=size_b,
            size_h=human_size(size_b),
            thumb=thumb,
            dlink=dlink,
            direct_link=resolved
        )
        return meta, None

# ---------- download (supports both requests and SimpleRequests) ----------
async def download_to_file(url: str, dest_path: str) -> bool:
    if HAVE_REQUESTS:
        def _save_requests():
            try:
                with requests.get(url, headers=_headers(), stream=True) as r:  # type: ignore
                    sc = getattr(r, "status_code", 200)
                    if sc >= 400:
                        return False
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 512):
                            if not chunk:
                                continue
                            f.write(chunk)
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_save_requests)
    else:
        def _save_simple():
            try:
                r = requests.get(url, headers=_headers())  # type: ignore
                if getattr(r, "status_code", 200) >= 400:
                    return False
                with open(dest_path, "wb") as f:
                    f.write(getattr(r, "content", b""))
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_save_simple)

# ---------- Bot UI / Handlers ----------
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

router = Router()
MAX_DL_BYTES = 500 * 1024 * 1024  # 500MB

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Help", callback_data="help"),
         InlineKeyboardButton(text="👮 Police", callback_data="police")],
    ])

def kb_actions(direct_link: Optional[str], url_hex: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if direct_link:
        rows.append([InlineKeyboardButton(text="🔗 Direct Link", url=direct_link)])
    rows.append([
        InlineKeyboardButton(text="⬇️ Download", callback_data=f"dl:{url_hex}"),
        InlineKeyboardButton(text="📖 Help", callback_data="help")
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

HELP_TEXT = (
    "<b>How to use</b>\n"
    "• Send a TeraBox link (terabox.com / teraboxapp.com / etc.).\n"
    "• I’ll show file name + size, a <b>Direct Link</b> button, and a <b>Download</b> option (≤ 500 MB).\n\n"
    "<b>Notes</b>\n"
    "• If direct link fails, the share may be private/expired or TeraBox changed their endpoints.\n"
    "• Run only one instance to avoid <i>Conflict: terminated by other getUpdates</i>.\n"
    "• Optional: add <code>TERABOX_COOKIE=...</code> in .env for some private shares.\n\n"
    "<b>Commands</b>\n"
    "/start — Welcome\n"
    "/help — How to use\n"
    "/status — VPS status (👮 Police)\n"
)

WELCOME = (
    "👋 <b>Welcome to TeraBox Downloader</b>\n"
    "Send a TeraBox share link; I’ll fetch details & give you a Direct Link.\n"
    "Tap <b>Download</b> to receive the file here (≤ 500 MB)."
)

@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(WELCOME, parse_mode=ParseMode.HTML, reply_markup=kb_main())

@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb_main())

@router.message(Command("status"))
async def cmd_status(m: Message):
    used, total = mem_info()
    dused, dtotal = disk_info()
    host = socket.gethostname()
    txt = (
        "👮 <b>Police Check — VPS Status</b>\n"
        f"• Host: <code>{host}</code>\n"
        f"• OS: <code>{platform.system()} {platform.release()}</code>\n"
        f"• Python: <code>{platform.python_version()}</code>\n"
        f"• Uptime: <code>{uptime_str()}</code>\n"
        f"• RAM: <code>{used} / {total}</code>\n"
        f"• Disk: <code>{dused} / {dtotal}</code>\n"
    )
    await m.answer(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main())

@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.message.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb_main())
    await cq.answer()

@router.callback_query(F.data == "police")
async def cb_police(cq: CallbackQuery):
    used, total = mem_info()
    dused, dtotal = disk_info()
    host = socket.gethostname()
    txt = (
        "👮 <b>Police Check — VPS Status</b>\n"
        f"• Host: <code>{host}</code>\n"
        f"• OS: <code>{platform.system()} {platform.release()}</code>\n"
        f"• Python: <code>{platform.python_version()}</code>\n"
        f"• Uptime: <code>{uptime_str()}</code>\n"
        f"• RAM: <code>{used} / {total}</code>\n"
        f"• Disk: <code>{dused} / {dtotal}</code>\n"
    )
    try:
        await cq.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    except Exception:
        await cq.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    await cq.answer()

@router.callback_query(F.data == "back")
async def cb_back(cq: CallbackQuery):
    try:
        await cq.message.edit_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    except Exception:
        await cq.message.answer(WELCOME, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    await cq.answer()

@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cq: CallbackQuery):
    try:
        raw = cq.data.split(":", 1)[1]
        url = bytes.fromhex(raw).decode("utf-8", "replace")
    except Exception:
        await cq.answer("Bad link.", show_alert=True)
        return

    await cq.answer("Downloading…")
    await cq.message.answer("⏬ Starting download… This may take a while for large files.")
    meta, err = await asyncio.to_thread(TeraBox.fetch_meta, url)
    if err or not meta:
        await cq.message.answer(f"❌ {err or 'Failed to fetch metadata.'}")
        return
    if meta.size_bytes > MAX_DL_BYTES:
        await cq.message.answer(
            f"❌ File too large for upload (limit 500MB). This file: <b>{meta.size_h}</b>",
            parse_mode=ParseMode.HTML
        )
        return
    best = meta.direct_link or meta.dlink
    if not best:
        await cq.message.answer("❌ Couldn't extract a direct link. The file may be private or format changed.")
        return

    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", meta.file_name or "file")
    dest = f"./{int(time.time())}_{safe_name}"
    ok = await download_to_file(best, dest)
    if not ok:
        await cq.message.answer("❌ Download failed (network / link issue).")
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        return

    try:
        await cq.message.answer_document(FSInputFile(dest), caption=f"{safe_name}\nSize: {meta.size_h}")
    except Exception as e:
        await cq.message.answer(f"❌ Upload failed: {e}")
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass

@router.message(F.text)
async def on_text(m: Message):
    url = extract_first_url(m.text or "")
    if not url:
        if m.chat.type == ChatType.PRIVATE:
            await m.answer("Send me a TeraBox link to get started. /help")
        return

    meta, err = await asyncio.to_thread(TeraBox.fetch_meta, url)
    if err or not me
