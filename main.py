# Fragment TON Connect Test Bot v5
# Edit BOT_TOKEN below, then: python3 main.py

import asyncio
import json
import logging
import os
import re
import sys

BOT_TOKEN = "8516330436:AAGQ89mcJ_NqLpAbc3X1I22TNxDMadGdikk"
API_ID = 29060335
API_HASH = "b5b12f67224082319e736dc900a2f604"
OWNER_ID = 7940894807

try:
    import httpx
except ImportError:
    sys.exit("Run: pip install httpx")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Run: pip install beautifulsoup4")

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("frag")

FRAGMENT_BASE = "https://fragment.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CLIPBOARD_HOOK = """
() => {
    window.__copied_link = window.__copied_link || '';
    if (!window.__clipboard_hooked) {
        window.__clipboard_hooked = true;
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async (text) => {
            window.__copied_link = text;
            console.log('CLIPBOARD CAPTURED:', text.substring(0, 80));
            return orig(text);
        };
        // Also hook execCommand('copy') fallback
        const origExec = document.execCommand.bind(document);
        document.execCommand = function(cmd) {
            if (cmd === 'copy') {
                const sel = window.getSelection();
                if (sel) window.__copied_link = sel.toString();
            }
            return origExec.apply(document, arguments);
        };
    }
}
"""


class State:
    cookies = {}
    numbers = []
    page_hash = None
    connected = False
    browser_task = None

state = State()


def save_cookies_file(cookies, path="frag_export.json"):
    chromium = []
    for name, value in cookies.items():
        chromium.append({
            "name": name, "value": value,
            "domain": ".fragment.com", "path": "/",
            "secure": True, "httpOnly": name.startswith("stel_"),
        })
    with open(path, "w") as f:
        json.dump(chromium, f, indent=2)
    return path


def format_number(number):
    clean = number.replace(" ", "").replace("-", "")
    if clean.startswith("+888") and len(clean) >= 12:
        return clean[:4] + " " + clean[4:8] + " " + clean[8:]
    return clean


async def inject_hooks(page):
    try:
        await page.evaluate(CLIPBOARD_HOOK)
    except Exception as e:
        log.debug("Hook inject: %s", e)


async def get_captured_link(page):
    try:
        val = await page.evaluate("() => window.__copied_link || ''")
        if val and len(val) > 20:
            return val
    except Exception:
        pass
    return None


async def find_and_click_copy_icon(page):
    # The copy icon is a small clipboard icon at bottom-right of the QR code area
    # From screenshots: it's a small SVG/element ~30x30px near the QR container edge

    # Method 1: Find by element properties
    result = await page.evaluate("""
        () => {
            const found = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const rect = el.getBoundingClientRect();
                // Small element (icon-sized: 15-50px)
                if (rect.width < 15 || rect.width > 55) continue;
                if (rect.height < 15 || rect.height > 55) continue;
                // Must be visible
                if (rect.x <= 0 || rect.y <= 0) continue;
                // Must be in the modal area (center-right of screen)
                if (rect.x < 300 || rect.y < 100 || rect.y > 600) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                if (parseFloat(style.opacity) < 0.1) continue;

                // Check if it has click handler or is interactive
                const tag = el.tagName.toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const cursor = style.cursor;

                // Prefer elements that look clickable
                const isClickable = (
                    tag === 'button' || tag === 'svg' || tag === 'a' ||
                    role === 'button' || cursor === 'pointer' ||
                    cls.includes('copy') || cls.includes('icon') || cls.includes('btn')
                );

                found.push({
                    tag: tag,
                    cls: cls.substring(0, 60),
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                    clickable: isClickable,
                    cursor: cursor,
                });
            }
            // Sort: clickable first, then by x position (rightmost = more likely copy icon)
            found.sort((a, b) => {
                if (a.clickable !== b.clickable) return b.clickable - a.clickable;
                return b.x - a.x;
            });
            return found.slice(0, 20);
        }
    """)

    log.info("Found %d small elements", len(result))
    for el in result[:5]:
        log.info("  %s.%s at (%d,%d) %dx%d clickable=%s cursor=%s",
                 el['tag'], el['cls'][:20], el['x'], el['y'], el['w'], el['h'],
                 el['clickable'], el['cursor'])

    return result


async def run_connect(bot, chat_id, msg_id):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        await bot.edit_message_text(chat_id, msg_id, "pip install playwright && playwright install chromium")
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = await context.new_page()

            # Step 1: Load Fragment
            await bot.edit_message_text(chat_id, msg_id,
                "<b>Step 1/5:</b> Loading fragment.com...", parse_mode=ParseMode.HTML)
            await page.goto(FRAGMENT_BASE, wait_until="networkidle")
            await asyncio.sleep(2)
            await inject_hooks(page)

            # Step 2: Click "Connect TON"
            await bot.edit_message_text(chat_id, msg_id,
                "<b>Step 2/5:</b> Clicking Connect TON...", parse_mode=ParseMode.HTML)
            try:
                await page.locator("text=Connect TON").first.click(timeout=5000)
                log.info("Clicked Connect TON")
            except Exception as e:
                await page.screenshot(path="debug2.png")
                await bot.send_photo(chat_id, "debug2.png",
                    caption="No Connect TON button: " + str(e)[:200])
                await browser.close()
                return

            await asyncio.sleep(3)
            await inject_hooks(page)

            # Step 3: Find the copy icon and click it
            # From screenshots: small clipboard icon at bottom-right corner of QR
            await bot.edit_message_text(chat_id, msg_id,
                "<b>Step 3/5:</b> Extracting connect link...", parse_mode=ParseMode.HTML)

            tonkeeper_link = None

            for attempt in range(12):
                await inject_hooks(page)

                # Find all small clickable elements
                elements = await find_and_click_copy_icon(page)

                # Click each one and check if clipboard was captured
                for el in elements:
                    try:
                        await page.mouse.click(el['x'], el['y'])
                        await asyncio.sleep(0.8)
                        link = await get_captured_link(page)
                        if link:
                            tonkeeper_link = link
                            if tonkeeper_link.startswith("tc://"):
                                tonkeeper_link = "https://app.tonkeeper.com/ton-connect" + tonkeeper_link[4:]
                            log.info("GOT LINK by clicking %s at (%d,%d): %s",
                                     el['tag'], el['x'], el['y'], link[:80])
                            break
                    except Exception:
                        pass

                if tonkeeper_link:
                    break

                # Also check page source for universal link
                try:
                    source = await page.content()
                    pats = [
                        r'(https://app\.tonkeeper\.com/ton-connect\?[^"\'<>\s]+)',
                        r'(tc://\?[^"\'<>\s]+)',
                    ]
                    for pat in pats:
                        m = re.search(pat, source)
                        if m:
                            tonkeeper_link = m.group(1)
                            log.info("Got link from source: %s", tonkeeper_link[:80])
                            break
                    if tonkeeper_link:
                        break
                except Exception:
                    pass

                # Check localStorage/sessionStorage for TON Connect data
                try:
                    storage_link = await page.evaluate("""
                        () => {
                            const stores = [localStorage, sessionStorage];
                            for (const store of stores) {
                                for (let i = 0; i < store.length; i++) {
                                    const key = store.key(i);
                                    const val = store.getItem(key) || '';
                                    if (val.includes('tonkeeper') || val.includes('ton-connect') || key.includes('ton')) {
                                        // Try to find URL in the value
                                        const urlMatch = val.match(/(https:\/\/app\.tonkeeper\.com[^"'\\s]+)/);
                                        if (urlMatch) return urlMatch[1];
                                        const tcMatch = val.match(/(tc:\/\/[^"'\\s]+)/);
                                        if (tcMatch) return tcMatch[1];
                                    }
                                }
                            }
                            return '';
                        }
                    """)
                    if storage_link and len(storage_link) > 20:
                        tonkeeper_link = storage_link
                        log.info("Got link from storage: %s", storage_link[:80])
                        break
                except Exception:
                    pass

                if attempt == 5:
                    # Take debug screenshot halfway through
                    await page.screenshot(path="debug3_mid.png")
                    log.info("Mid-attempt screenshot saved")

                await asyncio.sleep(2)

            if tonkeeper_link:
                tonkeeper_link = tonkeeper_link.replace("&amp;", "&").strip()

            if not tonkeeper_link:
                await page.screenshot(path="debug4.png")
                with open("debug4.html", "w") as f:
                    f.write(await page.content())
                await bot.send_photo(chat_id, "debug4.png", caption="Cannot extract link")
                try:
                    await bot.send_document(chat_id, "debug4.html", caption="Page HTML")
                except Exception:
                    pass
                # Also send the element list as text
                elements = await find_and_click_copy_icon(page)
                debug_text = "Elements found:\n"
                for el in elements[:15]:
                    debug_text += (
                        str(el['tag']) + "." + str(el['cls'])[:30]
                        + " at (" + str(el['x']) + "," + str(el['y']) + ")"
                        + " " + str(el['w']) + "x" + str(el['h'])
                        + " ptr=" + str(el['cursor']) + "\n"
                    )
                await bot.send_message(chat_id, "<code>" + debug_text[:3000] + "</code>",
                    parse_mode=ParseMode.HTML)
                await browser.close()
                return

            log.info("Final link: %s", tonkeeper_link[:100])

            # Step 4: Send connect button
            await bot.edit_message_text(chat_id, msg_id,
                "<b>Step 4/5:</b> Tap below to connect:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Connect in Tonkeeper", url=tonkeeper_link)],
                    [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                ]),
            )

            # Step 5: Wait for approval
            connected = False
            for i in range(100):
                await asyncio.sleep(3)
                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                if cookie_dict.get("stel_ton_token"):
                    connected = True
                    state.cookies = cookie_dict
                    state.connected = True
                    log.info("Connected!")
                    break

                try:
                    if "/my/" in page.url:
                        connected = True
                        cookies = await context.cookies()
                        state.cookies = {c["name"]: c["value"] for c in cookies}
                        state.connected = True
                        break
                except Exception:
                    pass

                if i > 0 and i % 10 == 0:
                    try:
                        await bot.edit_message_text(chat_id, msg_id,
                            "<b>Step 5/5:</b> Waiting... (" + str(i*3) + "s / 300s)",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("Connect in Tonkeeper", url=tonkeeper_link)],
                                [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                            ]),
                        )
                    except Exception:
                        pass

            if not connected:
                await bot.edit_message_text(chat_id, msg_id, "Timeout. /connect to retry")
                await browser.close()
                return

            # Fetch numbers
            await bot.edit_message_text(chat_id, msg_id, "Connected! Fetching numbers...")
            await page.goto(FRAGMENT_BASE + "/my/numbers", wait_until="networkidle")
            await asyncio.sleep(2)

            html = await page.content()
            numbers = set()
            for match in re.findall(r"\+?888\d{4,15}", html):
                num = match if match.startswith("+") else "+" + match
                numbers.add(num)
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a[href*='/number/']"):
                href = a.get("href", "")
                m = re.search(r"(\+?888\d{4,15})", href)
                if m:
                    num = m.group(1)
                    if not num.startswith("+"):
                        num = "+" + num
                    numbers.add(num)
            state.numbers = sorted(numbers)

            m = re.search(r'"hash"\s*:\s*"([a-f0-9]{16,})"', await page.content())
            if m:
                state.page_hash = m.group(1)

            cookie_path = save_cookies_file(state.cookies, "frag_export.json")

            nums_text = ""
            if state.numbers:
                for n in state.numbers:
                    nums_text += "  <code>" + format_number(n) + "</code>\n"
            else:
                nums_text = "  No numbers found\n"

            tokens_text = ""
            for k, v in sorted(state.cookies.items()):
                tokens_text += "<code>" + k + "=" + v + "</code>\n"

            hash_text = ""
            if state.page_hash:
                hash_text = "\n<b>Hash:</b>\n<code>" + state.page_hash + "</code>\n"

            full_msg = (
                "<b>Wallet Connected!</b>\n\n"
                "<b>Numbers (" + str(len(state.numbers)) + "):</b>\n"
                + nums_text + "\n"
                + "<b>All Cookies:</b>\n" + tokens_text
                + hash_text + "\n"
                + "/cookies to download\n/numbers to refresh"
            )
            # Telegram message limit is 4096 chars
            if len(full_msg) > 4000:
                short_msg = (
                    "<b>Wallet Connected!</b>\n\n"
                    "<b>Numbers (" + str(len(state.numbers)) + "):</b>\n"
                    + nums_text + hash_text + "\n"
                    + "/cookies for full dump\n/numbers to refresh"
                )
                await bot.edit_message_text(chat_id, msg_id, short_msg, parse_mode=ParseMode.HTML)
                await bot.send_message(chat_id,
                    "<b>All Cookies:</b>\n" + tokens_text, parse_mode=ParseMode.HTML)
            else:
                await bot.edit_message_text(chat_id, msg_id, full_msg, parse_mode=ParseMode.HTML)
            await browser.close()

    except asyncio.CancelledError:
        log.info("Cancelled")
    except Exception as e:
        log.exception("Error")
        try:
            await bot.edit_message_text(chat_id, msg_id,
                "Error: <code>" + str(e)[:500] + "</code>\n\n/connect to retry",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


app = Client("fragment_connect_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)


@app.on_message(filters.command("start") & filters.private & filters.user(OWNER_ID))
async def start_cmd(client, message):
    await message.reply(
        "<b>Fragment TON Connect Bot</b>\n\n"
        "/connect - Connect wallet\n/status - Status\n"
        "/numbers - List numbers\n/cookies - Download cookies\n/hash - Page hash",
        parse_mode=ParseMode.HTML)


@app.on_message(filters.command("connect") & filters.private & filters.user(OWNER_ID))
async def connect_cmd(client, message):
    if state.browser_task and not state.browser_task.done():
        return await message.reply("Already connecting.")
    msg = await message.reply("Starting...")
    state.browser_task = asyncio.create_task(run_connect(client, message.chat.id, msg.id))


@app.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def status_cmd(client, message):
    if state.connected:
        stel = [k for k in state.cookies if k.startswith("stel_")]
        await message.reply("<b>Connected</b>\nNumbers: " + str(len(state.numbers))
            + "\nTokens: " + str(len(stel)), parse_mode=ParseMode.HTML)
    else:
        await message.reply("Not connected. /connect")


@app.on_message(filters.command("connect") & filters.private & filters.user(OWNER_ID))
async def connect_cmd(client, message):
    if state.browser_task and not state.browser_task.done():
        return await message.reply("Already connecting.")
    msg = await message.reply("Starting...")
    state.browser_task = asyncio.create_task(run_connect(client, message.chat.id, msg.id))


@app.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def status_cmd(client, message):
    if state.connected:
        stel = [k for k in state.cookies if k.startswith("stel_")]
        await message.reply("<b>Connected</b>\nNumbers: " + str(len(state.numbers))
            + "\nTokens: " + str(len(stel)), parse_mode=ParseMode.HTML)
    else:
        await message.reply("Not connected. /connect")


@app.on_message(filters.command("numbers") & filters.private & filters.user(OWNER_ID))
async def numbers_cmd(client, message):
    if not state.connected:
        return await message.reply("Not connected. /connect")
    msg = await message.reply("Fetching...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, cookies=state.cookies) as hc:
            resp = await hc.get(FRAGMENT_BASE + "/my/numbers",
                headers={"User-Agent": USER_AGENT, "Referer": FRAGMENT_BASE + "/"})
        numbers = set()
        if resp.status_code == 200:
            for match in re.findall(r"\+?888\d{4,15}", resp.text):
                num = match if match.startswith("+") else "+" + match
                numbers.add(num)
        state.numbers = sorted(numbers)
        if numbers:
            text = "<b>Numbers (" + str(len(numbers)) + "):</b>\n\n"
            for n in sorted(numbers):
                text += "  <code>" + format_number(n) + "</code>\n"
        else:
            text = "No numbers found."
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text("Error: " + str(e))


@app.on_message(filters.command("cookies") & filters.private & filters.user(OWNER_ID))
async def cookies_cmd(client, message):
    if not state.cookies:
        return await message.reply("No cookies. /connect first.")
    path = save_cookies_file(state.cookies, "frag_export.json")
    # Build full cookie output - ALL cookies, FULL values, monospace for copy-paste
    lines = ["<b>Fragment Cookies</b>\n"]
    for k, v in sorted(state.cookies.items()):
        lines.append("<code>" + k + "=" + v + "</code>")
    if state.page_hash:
        lines.append("\n<b>Hash:</b>")
        lines.append("<code>" + state.page_hash + "</code>")
    caption = "\n".join(lines)
    # Telegram caption limit is 1024 chars; if over, send as message instead
    if len(caption) > 1024:
        await message.reply_document(path, caption="<b>Fragment Cookies</b>",
            parse_mode=ParseMode.HTML)
        # Split into chunks if needed (Telegram message limit 4096)
        for i in range(0, len(caption), 4000):
            await message.reply(caption[i:i+4000], parse_mode=ParseMode.HTML)
    else:
        await message.reply_document(path, caption=caption, parse_mode=ParseMode.HTML)


@app.on_message(filters.command("hash") & filters.private & filters.user(OWNER_ID))
async def hash_cmd(client, message):
    msg = await message.reply("Fetching...")
    try:
        cookies = state.cookies if state.connected else {}
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, cookies=cookies) as hc:
            resp = await hc.get(FRAGMENT_BASE, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            text = resp.text
            patterns = [
                r'"hash"\s*:\s*"([a-f0-9]{16,})"',
                r"'hash'\s*:\s*'([a-f0-9]{16,})'",
                r'hash[=:]\s*["\']([a-f0-9]{16,})["\']',
                r'data-hash="([a-f0-9]{16,})"',
                r'ajax[^"]*hash[^"]*":\s*"([a-f0-9]{16,})"',
                r'\?hash=([a-f0-9]{16,})',
                r'hash=([a-f0-9]{16,})',
            ]
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    state.page_hash = m.group(1)
                    return await msg.edit_text(
                        "Hash:\n<code>" + m.group(1) + "</code>",
                        parse_mode=ParseMode.HTML)
            # Not found - send debug snippet
            # Look for anything containing "hash"
            hash_lines = []
            for line in text.split("\n"):
                if "hash" in line.lower() and len(line) < 500:
                    hash_lines.append(line.strip()[:200])
            if hash_lines:
                debug = "\n".join(hash_lines[:10])
                await msg.edit_text(
                    "Hash not found by pattern.\n\nLines containing 'hash':\n<code>"
                    + debug[:3000] + "</code>",
                    parse_mode=ParseMode.HTML)
            else:
                await msg.edit_text("Hash not found. No 'hash' references in page.")
        else:
            await msg.edit_text("HTTP " + str(resp.status_code))
    except Exception as e:
        await msg.edit_text("Error: " + str(e))

@app.on_callback_query(filters.regex("cancel_connect") & filters.user(OWNER_ID))
async def cancel_cb(client, query):
    if state.browser_task and not state.browser_task.done():
        state.browser_task.cancel()
        await query.message.edit_text("Cancelled.")
    await query.answer("Cancelled")


@app.on_message(filters.private & ~filters.user(OWNER_ID))
async def block_others(client, message):
    await message.reply("Private bot.")


if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("Edit BOT_TOKEN in main.py first!")
        sys.exit(1)
    try:
        from playwright.async_api import async_playwright
        print("Playwright: OK")
    except ImportError:
        print("WARNING: pip install playwright && playwright install chromium")
    print("Starting bot...")
    app.run()
