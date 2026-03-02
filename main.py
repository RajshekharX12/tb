# Fragment TON Connect Test Bot
# Setup: Edit BOT_TOKEN below, then run: python3 main.py

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
    wallet_address = None
    browser_task = None

state = State()


async def fetch_page_hash():
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(FRAGMENT_BASE, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        html = resp.text
        patterns = [
            r'"hash"\s*:\s*"([a-f0-9]{16,})"',
            r"'hash'\s*:\s*'([a-f0-9]{16,})'",
            r'data-hash="([a-f0-9]{16,})"',
        ]
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            text = script.get_text()
            for p in patterns:
                m = re.search(p, text)
                if m:
                    return m.group(1)
    return None


async def fetch_numbers_with_cookies(cookies):
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, cookies=cookies) as client:
        resp = await client.get(
            FRAGMENT_BASE + "/my/numbers",
            headers={"User-Agent": USER_AGENT, "Referer": FRAGMENT_BASE + "/"},
        )
        if resp.status_code != 200:
            return []
        html = resp.text
        if "/login" in str(resp.url).lower() or "sign in" in html.lower():
            return []
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
    return sorted(numbers)


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
            "pip install playwright && playwright install chromium",
            parse_mode=ParseMode.HTML,
        )
        return

    await bot.edit_message_text(
        chat_id, status_msg_id,
        "<b>Step 1/5:</b> Launching browser...",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            # Step 1: Load Fragment
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 1/5:</b> Loading fragment.com...",
                parse_mode=ParseMode.HTML,
            )
            await page.goto(FRAGMENT_BASE, wait_until="networkidle")
            await asyncio.sleep(2)

            # Step 2: Find login button
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 2/5:</b> Looking for TON Connect button...",
                parse_mode=ParseMode.HTML,
            )

            clicked = False
            selectors = [
                "text=Log In",
                "text=log in",
                "text=Connect",
                "text=Connect TON",
                "text=Connect Wallet",
                ".btn-primary",
                "button:has-text('Log')",
                "button:has-text('Connect')",
                "a:has-text('Log In')",
                ".header-login",
                ".auth-button",
            ]

            for sel in selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click()
                        clicked = True
                        log.info("Clicked: %s", sel)
                        break
                except Exception:
                    continue

            if not clicked:
                ss_path = "frag_step2_debug.png"
                await page.screenshot(path=ss_path)
                await bot.edit_message_text(
                    chat_id, status_msg_id,
                    "Could not find login button. Sending screenshot...",
                    parse_mode=ParseMode.HTML,
                )
                if os.path.exists(ss_path):
                    await bot.send_photo(chat_id, ss_path, caption="Fragment page - no login button found")
                await browser.close()
                return

            await asyncio.sleep(3)

            # Step 3: Find Tonkeeper link
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 3/5:</b> Extracting Tonkeeper link...",
                parse_mode=ParseMode.HTML,
            )

            tonkeeper_link = None

            for attempt in range(5):
                links = await page.evaluate("""
                    () => {
                        const results = [];
                        document.querySelectorAll('a').forEach(a => {
                            if (a.href) results.push({href: a.href, text: a.innerText});
                        });
                        document.querySelectorAll('[data-tc-url], [data-url], [data-href]').forEach(el => {
                            const url = el.dataset.tcUrl || el.dataset.url || el.dataset.href || '';
                            if (url) results.push({href: url, text: el.innerText});
                        });
                        document.querySelectorAll('*').forEach(el => {
                            for (const attr of el.attributes) {
                                if (attr.value && (attr.value.includes('ton-connect') || attr.value.includes('tonkeeper'))) {
                                    results.push({href: attr.value, text: attr.name});
                                }
                            }
                        });
                        return results;
                    }
                """)

                for link_info in links:
                    href = link_info.get("href", "")
                    if "tonkeeper" in href or "ton-connect" in href or href.startswith("tc://"):
                        tonkeeper_link = href
                        break

                if tonkeeper_link:
                    break

                tk_selectors = [
                    "text=Tonkeeper",
                    "img[alt*='Tonkeeper']",
                    "[data-wallet='tonkeeper']",
                ]
                for sel in tk_selectors:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            href = await el.get_attribute("href")
                            if href and ("tonkeeper" in href or "ton-connect" in href):
                                tonkeeper_link = href
                                break
                            parent_href = await el.evaluate("el => el.closest('a')?.href || ''")
                            if parent_href and ("tonkeeper" in parent_href or "ton-connect" in parent_href):
                                tonkeeper_link = parent_href
                                break
                    except Exception:
                        continue

                if tonkeeper_link:
                    break
                await asyncio.sleep(2)

            if not tonkeeper_link:
                page_source = await page.content()
                patterns = [
                    r'(https://app\.tonkeeper\.com/ton-connect[^"\'\s>]+)',
                    r'(tc://[^"\'\s>]+)',
                    r'(https://[^"\'\s>]*ton-connect[^"\'\s>]+)',
                ]
                for pat in patterns:
                    m = re.search(pat, page_source)
                    if m:
                        tonkeeper_link = m.group(1)
                        break

            if not tonkeeper_link:
                ss_path = "frag_step3_debug.png"
                await page.screenshot(path=ss_path)
                html_path = "frag_step3_debug.html"
                with open(html_path, "w") as f:
                    f.write(await page.content())
                await bot.edit_message_text(
                    chat_id, status_msg_id,
                    "Could not find Tonkeeper link. Sending debug files...",
                    parse_mode=ParseMode.HTML,
                )
                if os.path.exists(ss_path):
                    await bot.send_photo(chat_id, ss_path, caption="No Tonkeeper link found")
                if os.path.exists(html_path):
                    await bot.send_document(chat_id, html_path, caption="Page source for debugging")
                await browser.close()
                return

            tonkeeper_link = tonkeeper_link.replace("&amp;", "&")

            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 3/5:</b> Tonkeeper link found!\n\nTap the button below to connect your wallet:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open Tonkeeper", url=tonkeeper_link)],
                    [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                ]),
            )

            # Step 4: Wait for approval
            log.info("Waiting for Tonkeeper approval...")

            connected = False
            for i in range(100):
                await asyncio.sleep(3)

                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                if cookie_dict.get("stel_ton_token"):
                    connected = True
                    state.cookies = cookie_dict
                    state.connected = True
                    stel_cookies = {k: v for k, v in cookie_dict.items() if k.startswith("stel_")}
                    log.info("Connected! Cookies: %s", list(stel_cookies.keys()))
                    break

                if i > 0 and i % 10 == 0:
                    try:
                        await bot.edit_message_text(
                            chat_id, status_msg_id,
                            "<b>Step 4/5:</b> Waiting for Tonkeeper approval...\n"
                            "(" + str(i * 3) + "s elapsed, 5 min timeout)\n\n"
                            "Tap below if you haven't yet:",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("Open Tonkeeper", url=tonkeeper_link)],
                                [InlineKeyboardButton("Cancel", callback_data="cancel_connect")],
                            ]),
                        )
                    except Exception:
                        pass

            if not connected:
                await bot.edit_message_text(
                    chat_id, status_msg_id,
                    "Timeout: No wallet connection in 5 minutes.\nTry again with /connect",
                    parse_mode=ParseMode.HTML,
                )
                await browser.close()
                return

            # Step 5: Fetch numbers
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Step 5/5:</b> Fetching your numbers...",
                parse_mode=ParseMode.HTML,
            )

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

            page_source = await page.content()
            for pat in [r'"hash"\s*:\s*"([a-f0-9]{16,})"']:
                m = re.search(pat, page_source)
                if m:
                    state.page_hash = m.group(1)
                    break

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
                hash_text = "Page Hash: <code>" + state.page_hash[:15] + "...</code>\n\n"

            await bot.edit_message_text(
                chat_id, status_msg_id,
                "<b>Wallet Connected!</b>\n\n"
                "<b>Numbers (" + str(len(state.numbers)) + "):</b>\n" + nums_text + "\n"
                "<b>Tokens:</b>\n" + tokens_text + "\n"
                + hash_text +
                "Cookies saved to: <code>" + cookie_path + "</code>\n\n"
                "Use /cookies to get the file\n"
                "Use /numbers to refresh the list",
                parse_mode=ParseMode.HTML,
            )

            await browser.close()

    except asyncio.CancelledError:
        log.info("Connect flow cancelled")
    except Exception as e:
        log.exception("Playwright error")
        try:
            await bot.edit_message_text(
                chat_id, status_msg_id,
                "Error: <code>" + str(e)[:500] + "</code>\n\nTry again with /connect",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# Bot setup
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
        "Connect your Tonkeeper wallet to Fragment.com\n"
        "and automatically extract cookies + numbers.\n\n"
        "<b>Commands:</b>\n"
        "/connect - Start wallet connection\n"
        "/status - Current connection status\n"
        "/numbers - List numbers from wallet\n"
        "/cookies - Download cookies file\n"
        "/hash - Get Fragment page hash\n\n"
        "<i>Only you can use this bot.</i>",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("connect") & filters.private & filters.user(OWNER_ID))
async def connect_cmd(client, message):
    if state.browser_task and not state.browser_task.done():
        return await message.reply("A connection is already in progress.")

    status_msg = await message.reply(
        "Starting TON Connect flow...",
        parse_mode=ParseMode.HTML,
    )

    state.browser_task = asyncio.create_task(
        run_playwright_connect(client, message.chat.id, status_msg.id)
    )


@app.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def status_cmd(client, message):
    if state.connected:
        stel_keys = [k for k in state.cookies if k.startswith("stel_")]
        await message.reply(
            "<b>Connected</b>\n\n"
            "Numbers: " + str(len(state.numbers)) + "\n"
            "Cookies: " + str(len(stel_keys)) + " stel_* tokens\n"
            "Page Hash: " + ("Yes" if state.page_hash else "No"),
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply("Not connected. Use /connect to start.")


@app.on_message(filters.command("numbers") & filters.private & filters.user(OWNER_ID))
async def numbers_cmd(client, message):
    if not state.connected or not state.cookies:
        return await message.reply("Not connected. Use /connect first.")

    status = await message.reply("Fetching numbers from Fragment...")

    try:
        numbers = await fetch_numbers_with_cookies(state.cookies)
        state.numbers = numbers

        if numbers:
            text = "<b>Numbers (" + str(len(numbers)) + "):</b>\n\n"
            for n in numbers:
                text += "  <code>" + format_number(n) + "</code>\n"
        else:
            text = "No numbers found on this wallet."

        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status.edit_text("Error: " + str(e))


@app.on_message(filters.command("cookies") & filters.private & filters.user(OWNER_ID))
async def cookies_cmd(client, message):
    if not state.cookies:
        return await message.reply("No cookies available. Use /connect first.")

    path = save_cookies_file(state.cookies, "frag_export.json")

    ssid = state.cookies.get("stel_ssid", "N/A")
    token = state.cookies.get("stel_token", "N/A")
    ton_token = state.cookies.get("stel_ton_token", "N/A")
    ton_preview = ton_token[:30] + "..." if len(ton_token) > 30 else ton_token

    await message.reply_document(
        path,
        caption=(
            "<b>Fragment Cookies</b>\n\n"
            "Rename to frag.json and place in your bot directory.\n\n"
            "<b>Guard config values:</b>\n"
            "<code>GUARD_STEL_SSID=" + ssid + "</code>\n"
            "<code>GUARD_STEL_TON_TOKEN=" + ton_preview + "</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("hash") & filters.private & filters.user(OWNER_ID))
async def hash_cmd(client, message):
    status = await message.reply("Fetching Fragment page hash...")

    try:
        page_hash = await fetch_page_hash()
        if page_hash:
            state.page_hash = page_hash
            await status.edit_text(
                "<b>Fragment Page Hash:</b>\n\n"
                "<code>" + page_hash + "</code>\n\n"
                "Use this as FRAGMENT_API_HASH in config.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text("Could not extract hash from Fragment.")
    except Exception as e:
        await status.edit_text("Error: " + str(e))


@app.on_callback_query(filters.regex("cancel_connect") & filters.user(OWNER_ID))
async def cancel_connect_cb(client, query):
    if state.browser_task and not state.browser_task.done():
        state.browser_task.cancel()
        await query.message.edit_text("Connection cancelled.")
    await query.answer("Cancelled")


@app.on_message(filters.private & ~filters.user(OWNER_ID))
async def block_others(client, message):
    await message.reply("This bot is private.")


if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("Edit BOT_TOKEN in main.py first!")
        print("Get a token from @BotFather on Telegram.")
        sys.exit(1)

    try:
        from playwright.async_api import async_playwright
        print("Playwright: OK")
    except ImportError:
        print("WARNING: Playwright not installed!")
        print("Run: pip install playwright && playwright install chromium")

    print("Owner:", OWNER_ID)
    print("Starting bot...")
    app.run()
