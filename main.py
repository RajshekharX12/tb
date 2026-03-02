"""
Fragment TON Connect Test Bot
============================

Standalone bot to test connecting TON wallets to Fragment.com
and extracting cookies + numbers automatically.

Setup:
    1. Create a new bot via @BotFather → get token
    2. Edit BOT_TOKEN and OWNER_ID below
    3. pip install pyrogram tgcrypto httpx beautifulsoup4 playwright
    4. playwright install chromium
    5. python3 fragment_connect_bot.py

Commands:
    /start    — Welcome message
    /connect  — Start TON Connect flow (opens Tonkeeper)
    /status   — Show current wallet status
    /numbers  — Fetch numbers from connected wallet
    /cookies  — Export cookies as frag.json
    /hash     — Extract Fragment page hash
"""

import asyncio
import json
import logging
import os
import re
import time
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

# ══════════════════════════════════════════
# CONFIG — EDIT THESE
# ══════════════════════════════════════════

BOT_TOKEN = "8516330436:AAGQ89mcJ_NqLpAbc3X1I22TNxDMadGdikk"
API_ID = 29060335
API_HASH = "b5b12f67224082319e736dc900a2f604"
OWNER_ID = 7940894807  # Only you can use this bot

# ══════════════════════════════════════════

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
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frag_connect")

# ══════════════════════════════════════════
# STATE
# ══════════════════════════════════════════

class State:
    """Holds the current connection state."""
    cookies: Dict[str, str] = {}
    numbers: List[str] = []
    page_hash: Optional[str] = None
    connected: bool = False
    wallet_address: Optional[str] = None
    connect_session: Dict = {}  # TON Connect session data
    browser_task: Optional[asyncio.Task] = None

state = State()

FRAGMENT_BASE = "https://fragment.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ══════════════════════════════════════════
# FRAGMENT HELPERS
# ══════════════════════════════════════════

async def fetch_page_hash() -> Optional[str]:
    """Load fragment.com and extract the API hash from page source."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(
            FRAGMENT_BASE,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return None

        html = resp.text
        patterns = [
            r'"hash"\s*:\s*"([a-f0-9]{16,})"',
            r"'hash'\s*:\s*'([a-f0-9]{16,})'",
            r'hash["\s]*[:=]\s*["\']([a-f0-9]{16,})["\']',
            r'data-hash="([a-f0-9]{16,})"',
        ]
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)

        # Try inside script tags
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            text = script.get_text()
            for p in patterns:
                m = re.search(p, text)
                if m:
                    return m.group(1)
    return None


async def fetch_numbers_with_cookies(cookies: Dict[str, str]) -> List[str]:
    """Fetch /my/numbers using provided cookies."""
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        cookies=cookies,
    ) as client:
        resp = await client.get(
            f"{FRAGMENT_BASE}/my/numbers",
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"{FRAGMENT_BASE}/",
            },
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


def save_cookies_file(cookies: Dict[str, str], path: str = "frag_export.json") -> str:
    """Save cookies as Chromium JSON format."""
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


def format_number(number: str) -> str:
    """Format +888XXXXXXXX to +888 XXXX XXXX."""
    clean = number.replace(" ", "").replace("-", "")
    if clean.startswith("+888") and len(clean) >= 12:
        return f"{clean[:4]} {clean[4:8]} {clean[8:]}"
    return clean


# ══════════════════════════════════════════
# PLAYWRIGHT — BROWSER AUTOMATION
# ══════════════════════════════════════════

async def run_playwright_connect(app: Client, chat_id: int, status_msg_id: int):
    """
    Full Playwright flow:
    1. Open Fragment → click Login
    2. Find Tonkeeper link → send to user
    3. Wait for user to approve → detect cookies
    4. Fetch numbers → report results
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        await app.edit_message_text(
            chat_id, status_msg_id,
            "❌ Playwright not installed.\n\n"
            "Run on your server:\n"
            "<code>pip install playwright\nplaywright install chromium</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await app.edit_message_text(
        chat_id, status_msg_id,
        "⏳ <b>Step 1/5:</b> Launching browser...",
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

            # ── Step 1: Load Fragment ──
            await app.edit_message_text(
                chat_id, status_msg_id,
                "⏳ <b>Step 1/5:</b> Loading fragment.com...",
                parse_mode=ParseMode.HTML,
            )
            await page.goto(FRAGMENT_BASE, wait_until="networkidle")
            await asyncio.sleep(2)

            # ── Step 2: Find and click login/connect ──
            await app.edit_message_text(
                chat_id, status_msg_id,
                "⏳ <b>Step 2/5:</b> Looking for TON Connect button...",
                parse_mode=ParseMode.HTML,
            )

            # Try multiple selectors for the login/connect button
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
                # Take screenshot for debugging
                ss_path = "frag_step2_debug.png"
                await page.screenshot(path=ss_path)
                await app.edit_message_text(
                    chat_id, status_msg_id,
                    "⚠️ Could not find login button.\n"
                    "Sending screenshot for debugging...",
                    parse_mode=ParseMode.HTML,
                )
                if os.path.exists(ss_path):
                    await app.send_photo(chat_id, ss_path, caption="Fragment page — no login button found")
                await browser.close()
                return

            await asyncio.sleep(3)

            # ── Step 3: Find Tonkeeper link ──
            await app.edit_message_text(
                chat_id, status_msg_id,
                "⏳ <b>Step 3/5:</b> Extracting Tonkeeper link...",
                parse_mode=ParseMode.HTML,
            )

            tonkeeper_link = None

            # Method 1: Look for Tonkeeper in the modal
            for attempt in range(5):
                # Check all links on page
                links = await page.evaluate("""
                    () => {
                        const results = [];
                        // Check all anchor tags
                        document.querySelectorAll('a').forEach(a => {
                            if (a.href) results.push({type: 'a', href: a.href, text: a.innerText});
                        });
                        // Check all elements with data attributes
                        document.querySelectorAll('[data-tc-url], [data-url], [data-href]').forEach(el => {
                            const url = el.dataset.tcUrl || el.dataset.url || el.dataset.href || '';
                            if (url) results.push({type: 'data', href: url, text: el.innerText});
                        });
                        // Check for universal links in any attribute
                        document.querySelectorAll('*').forEach(el => {
                            for (const attr of el.attributes) {
                                if (attr.value && attr.value.includes('ton-connect')) {
                                    results.push({type: 'attr', href: attr.value, text: attr.name});
                                }
                                if (attr.value && attr.value.includes('tonkeeper')) {
                                    results.push({type: 'attr', href: attr.value, text: attr.name});
                                }
                            }
                        });
                        return results;
                    }
                """)

                for link_info in links:
                    href = link_info.get("href", "")
                    if "tonkeeper" in href or "ton-connect" in href:
                        tonkeeper_link = href
                        break
                    if href.startswith("tc://"):
                        tonkeeper_link = href
                        break

                if tonkeeper_link:
                    break

                # Try clicking Tonkeeper option if modal is open
                tk_selectors = [
                    "text=Tonkeeper",
                    "img[alt*='Tonkeeper']",
                    "[data-wallet='tonkeeper']",
                    ".wallet-item:has-text('Tonkeeper')",
                ]
                for sel in tk_selectors:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            # Check if it has an href
                            href = await el.get_attribute("href")
                            if href and ("tonkeeper" in href or "ton-connect" in href):
                                tonkeeper_link = href
                                break
                            # Try parent link
                            parent_href = await el.evaluate("el => el.closest('a')?.href || ''")
                            if parent_href and ("tonkeeper" in parent_href or "ton-connect" in parent_href):
                                tonkeeper_link = parent_href
                                break
                    except Exception:
                        continue

                if tonkeeper_link:
                    break

                await asyncio.sleep(2)

            # Also try extracting from page source (sometimes link is in JS)
            if not tonkeeper_link:
                page_source = await page.content()
                patterns = [
                    r'(https://app\.tonkeeper\.com/ton-connect[^"\'>\s]+)',
                    r'(tc://[^"\'>\s]+)',
                    r'(https://[^"\'>\s]*ton-connect[^"\'>\s]+)',
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

                await app.edit_message_text(
                    chat_id, status_msg_id,
                    "⚠️ Could not find Tonkeeper link.\n"
                    "Sending debug files...",
                    parse_mode=ParseMode.HTML,
                )
                if os.path.exists(ss_path):
                    await app.send_photo(chat_id, ss_path, caption="Fragment modal — no Tonkeeper link found")
                if os.path.exists(html_path):
                    await app.send_document(chat_id, html_path, caption="Page source for debugging")
                await browser.close()
                return

            # ── Send link to user ──
            # Clean up the link (unescape if needed)
            tonkeeper_link = tonkeeper_link.replace("&amp;", "&")

            await app.edit_message_text(
                chat_id, status_msg_id,
                f"✅ <b>Step 3/5:</b> Tonkeeper link found!\n\n"
                f"👇 <b>Tap the button below to connect your wallet:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Open Tonkeeper", url=tonkeeper_link)],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_connect")],
                ]),
            )

            # ── Step 4: Wait for approval ──
            log.info("Waiting for Tonkeeper approval...")
            log.info("Link: %s", tonkeeper_link[:100])

            connected = False
            for i in range(100):  # 100 × 3s = 5 min
                await asyncio.sleep(3)

                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                # Check if stel_ton_token appeared (= wallet connected)
                if cookie_dict.get("stel_ton_token"):
                    connected = True
                    state.cookies = cookie_dict
                    state.connected = True

                    # Extract stel cookies
                    stel_cookies = {
                        k: v for k, v in cookie_dict.items()
                        if k.startswith("stel_")
                    }
                    log.info("Connected! Cookies: %s", list(stel_cookies.keys()))
                    break

                # Progress update every 30 seconds
                if i > 0 and i % 10 == 0:
                    try:
                        await app.edit_message_text(
                            chat_id, status_msg_id,
                            f"⏳ <b>Step 4/5:</b> Waiting for Tonkeeper approval...\n"
                            f"<i>({i * 3}s elapsed, 5 min timeout)</i>\n\n"
                            f"👇 Tap below if you haven't yet:",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔗 Open Tonkeeper", url=tonkeeper_link)],
                                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_connect")],
                            ]),
                        )
                    except Exception:
                        pass

            if not connected:
                await app.edit_message_text(
                    chat_id, status_msg_id,
                    "❌ <b>Timeout:</b> No wallet connection received in 5 minutes.\n\n"
                    "Try again with /connect",
                    parse_mode=ParseMode.HTML,
                )
                await browser.close()
                return

            # ── Step 5: Fetch numbers ──
            await app.edit_message_text(
                chat_id, status_msg_id,
                "⏳ <b>Step 5/5:</b> Fetching your numbers...",
                parse_mode=ParseMode.HTML,
            )

            await page.goto(f"{FRAGMENT_BASE}/my/numbers", wait_until="networkidle")
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

            # Also try to extract the page hash for API calls
            page_source = await page.content()
            for pat in [r'"hash"\s*:\s*"([a-f0-9]{16,})"', r"hash['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,})"]:
                m = re.search(pat, page_source)
                if m:
                    state.page_hash = m.group(1)
                    break

            # Save cookies
            cookie_path = save_cookies_file(state.cookies, "frag_export.json")

            # Get important tokens
            stel_tokens = {
                k: v for k, v in state.cookies.items()
                if k.startswith("stel_")
            }

            # ── Report results ──
            nums_text = ""
            if state.numbers:
                for n in state.numbers:
                    nums_text += f"  <code>{format_number(n)}</code>\n"
            else:
                nums_text = "  <i>No numbers found</i>\n"

            tokens_text = ""
            for k, v in stel_tokens.items():
                preview = v[:25] + "..." if len(v) > 25 else v
                tokens_text += f"  <b>{k}:</b> <code>{preview}</code>\n"

            await app.edit_message_text(
                chat_id, status_msg_id,
                f"✅ <b>Wallet Connected Successfully!</b>\n\n"
                f"📞 <b>Numbers ({len(state.numbers)}):</b>\n{nums_text}\n"
                f"🔑 <b>Tokens:</b>\n{tokens_text}\n"
                f"{'📄 <b>Page Hash:</b> <code>' + state.page_hash[:15] + '...</code>' if state.page_hash else ''}\n\n"
                f"📁 Cookies saved to: <code>{cookie_path}</code>\n\n"
                f"Use /cookies to get the file\n"
                f"Use /numbers to refresh the number list",
                parse_mode=ParseMode.HTML,
            )

  @app.on_message(filters.command("hash") & filters.private & filters.user(OWNER_ID))
async def hash_cmd(client: Client, message: Message):
    status = await message.reply(
        "⏳ Fetching Fragment page hash...",
        parse_mode=ParseMode.HTML,
    )

    try:
        page_hash = await fetch_page_hash()
        if page_hash:
            state.page_hash = page_hash
            await status.edit_text(
                f"✅ <b>Fragment Page Hash:</b>\n\n"
                f"<code>{page_hash}</code>\n\n"
                f"Use this as <code>FRAGMENT_API_HASH</code> in config.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text(
                "❌ Could not extract hash from Fragment.\n"
                "The page structure may have changed.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        await status.edit_text(
            f"❌ Error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


@app.on_callback_query(filters.regex("cancel_connect") & filters.user(OWNER_ID))
async def cancel_connect_cb(client: Client, query: CallbackQuery):
    if state.browser_task and not state.browser_task.done():
        state.browser_task.cancel()
        await query.message.edit_text(
            "❌ Connection cancelled.",
            parse_mode=ParseMode.HTML,
        )
    await query.answer("Cancelled")


@app.on_message(filters.private & ~filters.user(OWNER_ID))
async def block_others(client: Client, message: Message):
    await message.reply("🔒 This bot is private.")


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

if __name__ == "__main__":
    print("╔═══════════════════════════════════════════════╗")
    print("║  Fragment TON Connect Bot — Starting...        ║")
    print("╚═══════════════════════════════════════════════╝")
    print()

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Edit BOT_TOKEN in the script first!")
        print("   Get a token from @BotFather on Telegram.")
        sys.exit(1)

    # Check playwright
    try:
        from playwright.async_api import async_playwright
        print("✅ Playwright installed")
    except ImportError:
        print("⚠️  Playwright not installed!")
        print("   Run: pip install playwright && playwright install chromium")
        print("   The bot will start but /connect won't work without it.")
        print()

    print(f"👤 Owner: {OWNER_ID}")
    print()

    app.run()
