#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeraBox Downloader Bot (aiogram v3, single-file)
------------------------------------------------
• Reads ONLY BOT_TOKEN from .env (optional; falls back to environment variable)
• Paste your TeraBox link — bot returns info + a direct link button + optional download
• Clean, resilient aiogram v3 setup with sane defaults to avoid common errors
• Inline UI with "Direct Link", "Download", "Help", "Back", and a quick "👮 Police" status

Notes:
- Direct-link extraction relies on TeraBox public endpoints. If TeraBox changes internals, links
  may fail without an authentication cookie. You can optionally place TERABOX_COOKIE=... in .env,
  though only BOT_TOKEN is required. If TERABOX_COOKIE is absent, we try best-effort.
- For very large files, Telegram may reject uploads; we keep a conservative default limit.
- Make sure only ONE instance is running to avoid "Conflict: terminated by other getUpdates" errors.

Tested with: aiogram >= 3.4
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
from urllib.parse import urlparse, parse_qs, quote

# ---- minimal .env loader (no external deps) ----
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
        pass

load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TERABOX_COOKIE = os.environ.get("TERABOX_COOKIE", "").strip()  # optional

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN. Put it in .env as BOT_TOKEN=XXXX or set env var.")

# ---- requests w/o external deps: use stdlib http.client? No: easier to bundle 'requests'-like via urllib ----
# We'll use 'requests' only if present; otherwise fall back to urllib.
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
        def __init__(self):
            pass

        def request(self, method, url, headers=None, data=None, params=None, stream=False, allow_redirects=True):
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

        def get(self, url, headers=None, params=None, stream=False, allow_redirects=True):
            return self.request("GET", url, headers=headers, params=params, stream=stream, allow_redirects=allow_redirects)

        def head(self, url, headers=None, allow_redirects=False):
            # urllib doesn't have HEAD easily; we'll do GET with Range: bytes=0-0
            hdrs = dict(headers or {})
            hdrs["Range"] = "bytes=0-0"
            return self.request("GET", url, headers=hdrs, allow_redirects=allow_redirects)

        def post(self, url, headers=None, data=None, params=None):
            return self.request("POST", url, headers=headers, data=data, params=params)

    requests = SimpleRequests()  # type: ignore

# ---- aiogram v3 setup ----
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile, FSInputFile
)

# ---- Utilities ----
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
        if is_terabox_url(u):
            return u.strip(").,>\"\n\r\t")
    return None

def extract_surl(url: str) -> Optional[str]:
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        if "surl" in q and len(q["surl"]) > 0:
            return q["surl"][0]
        # fallback: /s/<code>
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
    i = int(math.floor(math.log(num, 1024)))
    i = min(i, len(units)-1)
    p = math.pow(1024, i)
    s = round(num / p, 2)
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

# ---- TeraBox client (best-effort scraping of public endpoints) ----
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
    dlink: Optional[str]        # probable pre-redirect link
    direct_link: Optional[str]  # resolved Location (if available)

class TeraBox:
    API_HOST = "https://www.terabox.com"

    @staticmethod
    def _request(method: str, url: str, **kwargs):
        if method == "GET":
            return requests.get(url, **kwargs)
        elif method == "POST":
            return requests.post(url, **kwargs)
        else:
            return requests.request(method, url, **kwargs)  # pragma: no cover

    @classmethod
    def fetch_page(cls, any_url: str):
        # Load the actual shared page to extract sign & timestamp
        return cls._request("GET", any_url, headers=_headers())

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
        r = cls._request("GET", url, headers=_headers(), params=params)
        try:
            return r.json()
        except Exception:
            return {}

    @classmethod
    def sharedownload(cls, surl: str, sign: str, ts: str, fs_id: int) -> Dict[str, Any]:
        # Two variants exist in the wild: fid_list and fidlist (TeraBox changes often).
        base_params = {
            "app_id": "250528",
            "web": "1",
            "channel": "share",
            "sign": sign,
            "timestamp": ts,
            "surl": surl
        }
        # Try fid_list first
        p1 = dict(base_params); p1["fid_list"] = f"[{fs_id}]"
        r1 = cls._request("GET", f"{cls.API_HOST}/api/sharedownload", headers=_headers(), params=p1)
        try:
            j1 = r1.json()
            if "dlink" in (j1.get("list") or [{}])[0]:
                return j1
        except Exception:
            pass
        # Try fidlist
        p2 = dict(base_params); p2["fidlist"] = f"[{fs_id}]"
        r2 = cls._request("GET", f"{cls.API_HOST}/api/sharedownload", headers=_headers(), params=p2)
        try:
            return r2.json()
        except Exception:
            return {}

    @staticmethod
    def parse_sign_ts(page_html: str) -> Tuple[Optional[str], Optional[str]]:
        # Try multiple patterns — TeraBox changes structure frequently
        patterns = [
            r'sign["\']?\s*[:=]\s*["\']([a-zA-Z0-9]+)["\']',
            r'window\.sign\s*=\s*["\']([a-zA-Z0-9]+)["\']',
        ]
        tspatts = [
            r'timestamp["\']?\s*[:=]\s*([0-9]{10,})',
            r'window\.timestamp\s*=\s*([0-9]{10,})',
        ]
        sign = ts = None
        for p in patterns:
            m = re.search(p, page_html)
            if m: sign = m.group(1); break
        for p in tspatts:
            m = re.search(p, page_html)
            if m: ts = m.group(1); break
        return sign, ts

    @classmethod
    def resolve_direct(cls, dlink: str) -> Optional[str]:
        # Follow redirect to get actual CDN link
        try:
            r = requests.head(dlink, headers=_headers(), allow_redirects=False)
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
            return None, "Could not parse the share code (surl)."

        # Load page to get sign & timestamp
        pg = cls.fetch_page(any_url)
        if pg.status_code != 200 or not pg.text:
            return None, "Failed to open the share page."

        sign, ts = cls.parse_sign_ts(pg.text)
        if not sign or not ts:
            # Sometimes we can still list, but download will miss. We'll carry on.
            pass

        # List files
        listing = cls.share_list(surl)
        if listing.get("errno", 0) != 0 or not listing.get("list"):
            return None, "API returned no files. Link may be private or expired."

        file_info = listing["list"][0]  # assume single file
        fs_id = int(file_info.get("fs_id", 0))
        size_b = int(file_info.get("size", 0))
        name = file_info.get("server_filename") or file_info.get("filename") or "file"
        thumb = (file_info.get("thumbs") or {}).get("url3")

        # Try getting a download link
        dlink = None
        resolved = None
        if sign and ts and fs_id:
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

# ---- Download helper (to disk) ----
async def download_to_file(url: str, dest_path: str) -> bool:
    def _save():
        try:
            with requests.get(url, headers=_headers(), stream=True) as r:
                if getattr(r, "status_code", 200) >= 400:
                    return False
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024*512) if hasattr(r, "iter_content") else [r.content]:
                        if not chunk:
                            continue
                        f.write(chunk)
            return True
        except Exception:
            return False
    return await asyncio.to_thread(_save)

# ---- Bot UI / Handlers ----
router = Router()

MAX_DL_BYTES = 500 * 1024 * 1024  # 500MB hard limit to keep uploads reliable

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Help", callback_data="help"),
         InlineKeyboardButton(text="👮 Police", callback_data="police")],
    ])

def kb_actions(direct_link: Optional[str], url_encoded: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if direct_link:
        rows.append([InlineKeyboardButton(text="🔗 Direct Link", url=direct_link)])
    rows.append([
        InlineKeyboardButton(text="⬇️ Download", callback_data=f"dl:{url_encoded}"),
        InlineKeyboardButton(text="📖 Help", callback_data="help")
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

HELP_TEXT = (
    "<b>How to use:</b>\n"
    "• Send me any TeraBox share link (terabox.com / teraboxapp.com, etc.).\n"
    "• I’ll fetch details, give you a <b>Direct Link</b>, and you can also tap <b>Download</b> to receive the file here (≤ 500 MB).\n\n"
    "<b>Tips</b>\n"
    "• If a direct link fails, the share may be <i>private, expired, or changed by TeraBox</i>.\n"
    "• Make sure only <b>one</b> bot instance is running, otherwise Telegram will show a <i>Conflict getUpdates</i> error.\n"
    "• Optional: you can put <code>TERABOX_COOKIE=...</code> in .env to increase success rate on some private shares.\n\n"
    "<b>Commands</b>\n"
    "/start — Welcome\n"
    "/help — This help\n"
    "/status — Quick VPS status (👮 Police)\n"
)

WELCOME = (
    "👋 <b>Welcome to TeraBox Downloader</b>\n"
    "Send a TeraBox link and I’ll fetch info & a direct link.\n"
    "Tap <b>Download</b> to get the file (≤ 500 MB).\n"
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

    await cq.answer("Downloading…", show_alert=False)
    await cq.message.answer("⏬ Starting download… This may take a while for large files.")

    meta, err = await asyncio.to_thread(TeraBox.fetch_meta, url)
    if err or not meta:
        await cq.message.answer(f"❌ {err or 'Failed to fetch metadata.'}")
        return
    if meta.size_bytes > MAX_DL_BYTES:
        await cq.message.answer(f"❌ File too large for upload. Limit is 500MB.\nThis file: <b>{meta.size_h}</b>", parse_mode=ParseMode.HTML)
        return
    best = meta.direct_link or meta.dlink
    if not best:
        await cq.message.answer("❌ Couldn't extract a direct link. The file may be private or TeraBox changed format.")
        return

    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", meta.file_name or "file")
    dest = f"./{int(time.time())}_{safe_name}"
    ok = await download_to_file(best, dest)
    if not ok:
        await cq.message.answer("❌ Download failed (network / link issue).")
        try:
            if os.path.exists(dest): os.remove(dest)
        except Exception:
            pass
        return

    try:
        await cq.message.answer_document(FSInputFile(dest), caption=f"{safe_name}\nSize: {meta.size_h}")
    except Exce
