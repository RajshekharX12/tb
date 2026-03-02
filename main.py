# Fragment TON Connect Test Bot
# Edit BOT_TOKEN below, then: python3 main.py

import asyncio
import json
import logging
import os
import re
import time
import sys
from typing import Dict, List, Optional

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("frag_connect")

FRAGMENT_BASE = "https://fragment.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
            "name": name,
            "value": value,
            "domain": ".fragment.com",
            "path": "/",
            "secure": True,
            "httpOnly": name.startswith("stel_"),
        })
    with open(path, "w") as f:
        json.dump(chromium, f, indent=2)
    return path


def format_number(number):
    clean = number.replace(" ", "").replace("-", "")
    if clean.startswith("+888") and len(clean) >= 12:
        return clean[:4] + " " + clean[4:8] + " " + clean[8:]
    return clean


async def run_playwright_connect(bot, chat_id, status_msg_id):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        await bot.edit_message_text(
            chat_id, status_msg_id,
            "pip install playwright and playwright install chromium",
        )
        return

    await bot.edit_message_text(
        chat_id, status_msg_id,
        "<b>Step 1/6:</b> Launching browser...",
        parse_mode=ParseMode.HTML,
    )

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
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 1/6:</b> Loading fragment.com...",
                parse_mode=ParseMode.HTML,
            )
            await page.goto(FRAGMENT_BASE, wait_until="networkidle")
            await asyncio.sleep(2)

            # Step 2: Click "Connect TON"
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 2/6:</b> Clicking Connect TON...",
                parse_mode=ParseMode.HTML,
            )
            try:
                btn = page.locator("text=Connect TON").first
                await btn.click(timeout=5000)
                log.info("Clicked Connect TON")
            except Exception as e:
                ss = "debug_step2.png"
                await page.screenshot(path=ss)
                await bot.send_photo(chat_id, ss, caption="Cannot find Connect TON: " + str(e)[:200])
                await browser.close()
                return

            await asyncio.sleep(3)

            # Step 3: Click Tonkeeper in the wallet modal
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 3/6:</b> Clicking Tonkeeper...",
                parse_mode=ParseMode.HTML,
            )
            await page.screenshot(path="debug_modal.png")

            tk_clicked = False
            try:
                tk = page.locator("text=Tonkeeper").first
                if await tk.count() > 0:
                    await tk.click(timeout=5000)
                    tk_clicked = True
                    log.info("Clicked Tonkeeper text")
            except Exception:
                pass

            if not tk_clicked:
                try:
                    await page.evaluate("""
                        () => {
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                if (el.textContent.trim() === 'Tonkeeper' && el.offsetParent !== null) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    tk_clicked = True
                    log.info("Clicked Tonkeeper via JS")
                except Exception:
                    pass

            if not tk_clicked:
                await bot.send_photo(chat_id, "debug_modal.png", caption="Cannot click Tonkeeper")
                await browser.close()
                return

            await asyncio.sleep(3)

            # Step 4: QR page - get the link
            # Screenshot 3 shows: QR code + "Open Link" + "Copy Link" buttons
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 4/6:</b> Getting connect link...",
                parse_mode=ParseMode.HTML,
            )
            await page.screenshot(path="debug_qr.png")

            tonkeeper_link = None

            for attempt in range(15):
                # Method 1: Click "Copy Link" and grab clipboard
                try:
                    copy_btn = page.locator("text=Copy Link").first
                    if await copy_btn.count() > 0 and await copy_btn.is_visible():
                        await copy_btn.click()
                        await asyncio.sleep(1)
                        clip = await page.evaluate("navigator.clipboard.readText()")
                        if clip and len(clip) > 20:
                            tonkeeper_link = clip
                            log.info("Got link from clipboard")
                            break
                except Exception as e:
                    log.debug("Clipboard fail: %s", e)

                # Method 2: "Open Link" href
                try:
                    open_btn = page.locator("text=Open Link").first
                    if await open_btn.count() > 0:
                        href = await open_btn.get_attribute("href")
                        if href and len(href) > 20:
                            tonkeeper_link = href
                            log.info("Got link from Open Link href")
                            break
                        href = await open_btn.evaluate("el => el.closest('a')?.href || ''")
                        if href and len(href) > 20:
                            tonkeeper_link = href
                            log.info("Got link from Open Link parent")
                            break
                except Exception:
                    pass

                # Method 3: Regex in page source
                try:
                    source = await page.content()
                    pats = [
                        r'(https://app\.tonkeeper\.com/ton-connect\?[^"\'<>\s]+)',
                        r'(tc://\?[^"\'<>\s]+)',
                        r'(https://[^"\'<>\s]*ton-connect/v2\?[^"\'<>\s]+)',
                    ]
                    for pat in pats:
                        m = re.search(pat, source)
                        if m:
                            tonkeeper_link = m.group(1)
                            log.info("Got link from regex")
                            break
                    if tonkeeper_link:
                        break
                except Exception:
                    pass

                # Method 4: Scan all <a> hrefs
                try:
                    hrefs = await page.evaluate("""
                        () => {
                            const h = [];
                            document.querySelectorAll('a[href]').forEach(a => {
                                if (a.href.length > 50) h.push(a.href);
                            });
                            document.querySelectorAll('*').forEach(el => {
                                for (const attr of el.attributes) {
                                    if (attr.value.length > 50 && (
                                        attr.value.includes('tonkeeper') ||
                                        attr.value.includes('ton-connect') ||
                                        attr.value.startsWith('tc://')
                                    )) {
                                        h.push(attr.value);
                                    }
                                }
                            });
                            return h;
                        }
                    """)
                    for href in hrefs:
                        if "tonkeeper" in href or "ton-connect" in href or "tc://" in href:
                            tonkeeper_link = href
                            log.info("Got link from href scan")
                            break
                    if tonkeeper_link:
                        break
                except Exception:
                    pass

                # Method 5: JS global variables
                try:
                    link_js = await page.evaluate("""
                        () => {
                            try {
                                if (window.tonConnectUI) {
                                    const c = window.tonConnectUI.connector;
                                    if (c && c.universalLink) return c.universalLink;
                                }
                            } catch(e) {}
                            return '';
                        }
                    """)
                    if link_js and len(link_js) > 20:
                        tonkeeper_link = link_js
                        log.info("Got link from JS global")
                        break
                except Exception:
                    pass

                await asyncio.sleep(2)

            if tonkeeper_link:
                tonkeeper_link = tonkeeper_link.replace("&amp;", "&").strip()

            if not tonkeeper_link:
                await page.screenshot(path="debug_step4.png")
                with open("debug_step4.html", "w") as f:
                    f.write(await page.content())
                await bot.send_photo(chat_id, "debug_step4.png", caption="QR visible but cannot get link")
                try:
                    await bot.send_document(chat_id, "debug_step4.html", caption="HTML source")
                except Exception:
                    pass
                await browser.close()
                return

            log.info("Link: %s", tonkeeper_link[:100])

            # Step 5: Send button to user
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 5/6:</b> Tap below to connect wallet:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Connect in Tonkeeper", url=tonkeeper_link)],
                    [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                ]),
            )

            # Step 6: Wait for approval
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
                        await bot.edit_message_text(
                            chat_id, status_msg_id,
                            "<b>Step 6/6:</b> Waiting... ("
                            + str(i * 3) + "s / 300s)",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("Connect in Tonkeeper", url=tonkeeper_link)],
                                [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                            ]),
                        )
                    except Exception:
                        pass

            if not connected:
                await bot.edit_message_text(chat_id, status_msg_id, "Timeout. /connect to retry")
                await browser.close()
                return

            # Fetch numbers
            await bot.edit_message_text(chat_id, status_msg_id, "Connected! Fetching numbers...")
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
            stel_tokens = {k: v for k, v in state.cookies.items() if k.startswith("stel_")}

            nums_text = ""
            if state.numbers:
                for n in state.numbers:
                    nums_text += "  <code>" + format_number(n) + "</code>\n"
            else:
                nums_text = "  No numbers found\n"

            tokens_text = ""
            for k, v in stel_tokens.items():
                preview = v[:25] + "..." if len(v) > 25 else v
                tokens_text += "  <b>" + k + ":</b> <code>" + preview + "</code>\n"

            hash_text = ""
            if state.page_hash:
                hash_text = "\nHash: <code>" + state.page_hash + "</code>\n"

            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Wallet Connected!</b>\n\n"
                "<b>Numbers (" + str(len(state.numbers)) + "):</b>\n"
                + nums_text + "\n"
                + "<b>Tokens:</b>\n" + tokens_text
                + hash_text + "\n"
                + "Use /cookies to download\nUse /numbers to refresh",
                parse_mode=ParseMode.HTML,
            )
            await browser.close()

    except asyncio.CancelledError:
        log.info("Cancelled")
    except Exception as e:
        log.exception("Error")
        try:
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "Error: <code>" + str(e)[:500] + "</code>\n\n/connect to retry",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


app = Client(
    "fragment_connect_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
)


@app.on_message(filters.command("start") & filters.private & filters.user(OWNER_ID))
async def start_cmd(client, message):
    await message.reply(
        "<b>Fragment TON Connect Bot</b>\n\n"
        "/connect - Connect wallet\n"
        "/status - Status\n"
        "/numbers - List numbers\n"
        "/cookies - Download cookies\n"
        "/hash - Get page hash",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("connect") & filters.private & filters.user(OWNER_ID))
async def connect_cmd(client, message):
    if state.browser_task and not state.browser_task.done():
        return await message.reply("Already connecting.")
    status_msg = await message.reply("Starting...")
    state.browser_task = asyncio.create_task(
        run_playwright_connect(client, message.chat.id, status_msg.id)
    )


@app.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def status_cmd(client, message):
    if state.connected:
        stel_keys = [k for k in state.cookies if k.startswith("stel_")]
        await message.reply(
            "<b>Connected</b>\nNumbers: " + str(len(state.numbers))
            + "\nTokens: " + str(len(stel_keys)),
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply("Not connected. /connect")


@app.on_message(filters.command("numbers") & filters.private & filters.user(OWNER_ID))
async def numbers_cmd(client, message):
    if not state.connected:
        return await message.reply("Not connected. /connect")
    status = await message.reply("Fetching...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, cookies=state.cookies) as hc:
            resp = await hc.get(
                FRAGMENT_BASE + "/my/numbers",
                headers={"User-Agent": USER_AGENT, "Referer": FRAGMENT_BASE + "/"},
            )
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
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status.edit_text("Error: " + str(e))


@app.on_message(filters.command("cookies") & filters.private & filters.user(OWNER_ID))
async def cookies_cmd(client, message):
    if not state.cookies:
        return await message.reply("No cookies. /connect first.")
    path = save_cookies_file(state.cookies, "frag_export.json")
    ssid = state.cookies.get("stel_ssid", "N/A")
    token = state.cookies.get("stel_token", "N/A")
    ton = state.cookies.get("stel_ton_token", "N/A")
    ton_p = ton[:30] + "..." if len(ton) > 30 else ton
    await message.reply_document(
        path,
        caption=(
            "<b>Fragment Cookies</b>\n\n"
            "<code>GUARD_STEL_SSID=" + ssid + "</code>\n"
            "<code>GUARD_STEL_TOKEN=" + token + "</code>\n"
            "<code>GUARD_STEL_TON_TOKEN=" + ton_p + "</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("hash") & filters.private & filters.user(OWNER_ID))
async def hash_cmd(client, message):
    status = await message.reply("Fetching...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
            resp = await hc.get(FRAGMENT_BASE, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            m = re.search(r'"hash"\s*:\s*"([a-f0-9]{16,})"', resp.text)
            if m:
                state.page_hash = m.group(1)
                return await status.edit_text(
                    "Hash:\n<code>" + m.group(1) + "</code>",
                    parse_mode=ParseMode.HTML,
                )
        await status.edit_text("Hash not found.")
    except Exception as e:
        await status.edit_text("Error: " + str(e))


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
