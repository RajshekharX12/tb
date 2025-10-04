from aiogram.client.default import DefaultBotPropertiesimport asyncio
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv
import httpx

from TeraboxDL import TeraboxDL  # pip install terabox-downloader

from .ui import kb_main_menu, text_help, text_privacy, text_welcome, text_limits
from .limits import RateLimiter, parse_size_mb
from .utils import human_size, is_video_ext

# ---------- ENV & CONFIG ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TERABOX_COOKIE = os.getenv("TERABOX_COOKIE", "").strip()  # e.g. "lang=en; ndus=xxxx;"
MAX_FILE_MB = parse_size_mb(os.getenv("MAX_FILE_MB", "1900"))  # keep < Telegram 2GB
USER_RATE_LIMIT = int(os.getenv("USER_RATE_LIMIT", "3"))       # downloads per hour per user
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))         # concurrent downloads
TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp/terabox_bot")
ALLOWED_DOMAINS = {"terabox.com", "terabox.app", "1024tera.com"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

os.makedirs(TMP_DIR, exist_ok=True)

# ---------- CORE OBJECTS ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
r = Router()
dp.include_router(r)

# download semaphore
dl_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
# simple per-user rate limiter (memory)
rate_limiter = RateLimiter(limit=USER_RATE_LIMIT, window_sec=3600)

# shared HTTP client for downloads (tuned for concurrency)
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, read=60.0),
    limits=httpx.Limits(max_keepalive_connections=MAX_CONCURRENT * 2, max_connections=MAX_CONCURRENT * 3),
    follow_redirects=True,
)

TERABOX_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:terabox\.(?:com|app)|1024tera\.com)/[^\s]+",
    re.IGNORECASE,
)

# ---------- HELPERS ----------
def is_allowed_domain(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower()
        return any(d in netloc for d in ALLOWED_DOMAINS)
    except Exception:
        return False

@asynccontextmanager
async def limited_download():
    await dl_semaphore.acquire()
    try:
        yield
    finally:
        dl_semaphore.release()

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="menu", description="Open menu"),
        BotCommand(command="help", description="How to use the bot"),
        BotCommand(command="limits", description="View limits & quotas"),
        BotCommand(command="privacy", description="Privacy & terms"),
    ])

async def extract_file_info(url: str) -> dict:
    """
    Use TeraboxDL to extract file metadata & direct URL.
    Requires TERABOX_COOKIE: 'lang=...; ndus=...;'
    """
    if not TERABOX_COOKIE:
        return {"error": "Missing TERABOX_COOKIE. Set it in your .env (see /help)."}
    tb = TeraboxDL(TERABOX_COOKIE)
    info = tb.get_file_info(url)
    return info

async def stream_download(direct_url: str, dest_path: str, progress_msg: Message, display_name: str, total_bytes: Optional[int]):
    """
    Stream download to dest_path and update Telegram message progress.
    """
    last_update = 0.0
    downloaded = 0
    started = time.time()

    async with http_client.stream("GET", direct_url) as resp:
        resp.raise_for_status()
        # try to deduce length if not provided
        if total_bytes is None:
            try:
                total_bytes = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total_bytes = 0

        with open(dest_path, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_update >= 2.0:
                    last_update = now
                    pct = (downloaded / total_bytes * 100) if total_bytes else 0
                    speed = downloaded / max(1e-6, (now - started))  # B/s
                    text = f"⬇️ *Downloading…*\n`{display_name}`\n" \
                           f"{human_size(downloaded)} / {human_size(total_bytes)}  ({pct:.1f}%)\n" \
                           f"Speed: {human_size(speed)}/s"
                    try:
                        await progress_msg.edit_text(text)
                    except Exception:
                        pass  # ignore edit rate limits

# ---------- UI ROUTES ----------
@r.message(CommandStart())
async def on_start(m: Message):
    await m.answer(text_welcome(), reply_markup=kb_main_menu())

@r.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("🧭 *Main Menu*", reply_markup=kb_main_menu())

@r.message(Command("help"))
async def on_help(m: Message):
    await m.answer(text_help(), disable_web_page_preview=True)

@r.message(Command("limits"))
async def on_limits(m: Message):
    await m.answer(text_limits(MAX_FILE_MB, USER_RATE_LIMIT, MAX_CONCURRENT))

@r.message(Command("privacy"))
async def on_privacy(m: Message):
    await m.answer(text_privacy(), disable_web_page_preview=True)

@r.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.edit_text(text_help(), disable_web_page_preview=True, reply_markup=kb_main_menu())
    await c.answer()

@r.callback_query(F.data == "limits")
async def cb_limits(c: CallbackQuery):
    await c.message.edit_text(text_limits(MAX_FILE_MB, USER_RATE_LIMIT, MAX_CONCURRENT), reply_markup=kb_main_menu())
    await c.answer()

@r.callback_query(F.data == "privacy")
async def cb_privacy(c: CallbackQuery):
    await c.message.edit_text(text_privacy(), disable_web_page_preview=True, reply_markup=kb_main_menu())
    await c.answer()

# ---------- MAIN HANDLER: TeraBox link in chat ----------
@r.message(F.text.regexp(TERABOX_URL_RE))
async def handle_terabox(m: Message):
    url = TERABOX_URL_RE.search(m.text).group(0).strip()

    if not is_allowed_domain(url):
        await m.reply("❌ That URL domain isn't supported.")
        return

    # basic per-user rate limit
    ok, wait_sec = rate_limiter.check(m.from_user.id)
    if not ok:
        await m.reply(f"⏳ Rate limit: try again in ~{int(wait_sec)}s.")
        return

    waiting = await m.reply("🔍 Fetching file info…")

    # 1) Extract metadata + direct URL
    info = await extract_file_info(url)
    if "error" in info:
        await waiting.edit_text(f"❌ *Error*: {info['error']}\n\nSet TERABOX_COOKIE (see /help).")
        return

    name = info.get("file_name") or "file"
    direct_url = info.get("download_link")
    size_bytes = info.get("sizebytes") or 0
    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0

    if not direct_url:
        await waiting.edit_text("❌ Couldn't extract a direct link. The file may be private or the link format changed.")
        return

    # 2) Enforce size limit
    if size_mb and size_mb > MAX_FILE_MB:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔗 Open Direct Link", url=direct_url)
        builder.adjust(1)
        await waiting.edit_text(
            f"⚠️ File is *too large* for Telegram upload.\n\n"
            f"*Name:* `{name}`\n*Size:* {human_size(size_bytes)}\n\n"
            f"I've provided a direct link instead.", reply_markup=builder.as_markup(), disable_web_page_preview=True
        )
        return

    # 3) Download + upload with auto-clean
    #    Keep server clean: use temp dir and delete after send.
    tmp_path = os.path.join(TMP_DIR, f"{int(time.time()*1000)}_{name}")
    progress = await waiting.edit_text(f"⬇️ Starting download…\n`{name}`")

    try:
        async with limited_download():
            await stream_download(
                direct_url=direct_url,
                dest_path=tmp_path,
                progress_msg=progress,
                display_name=name,
                total_bytes=size_bytes if size_bytes else None,
            )

        # 4) Send: choose best method
        caption = f"{name}\n{human_size(size_bytes)}"
        if is_video_ext(name):
            await m.answer_video(video=tmp_path, caption=caption)
        else:
            await m.answer_document(document=tmp_path, caption=caption)

        try:
            await progress.edit_text("✅ Sent successfully. (Cleaned temporary file.)")
        except Exception:
            pass
    except httpx.HTTPStatusError as e:
        await progress.edit_text(f"❌ HTTP error while downloading: {e.response.status_code}")
    except Exception as e:
        await progress.edit_text(f"❌ Error: {e}")
    finally:
        # Always clean temp file
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ---------- GENERIC TEXT (guidance) ----------
@r.message(F.text)
async def on_text(m: Message):
    await m.reply("Send a *TeraBox* share link to download.\nOpen /menu for help & limits.")

# ---------- ENTRY ----------
async def main():
    await set_commands()
    try:
        await dp.start_polling(bot)
    finally:
        await http_client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
