from aiogram.utils.keyboard import InlineKeyboardBuilder

def kb_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="❓ How to use", callback_data="help")
    kb.button(text="📏 Limits", callback_data="limits")
    kb.button(text="🔒 Privacy", callback_data="privacy")
    kb.adjust(2, 1)
    return kb.as_markup()

def text_welcome():
    return (
        "🎬 *TeraBox Downloader Bot*\n\n"
        "Paste a TeraBox share link and I’ll download & send the file.\n"
        "• Clean temp files automatically 🧹\n"
        "• Smart size limits & rate limits ⚖️\n"
        "• Direct link fallback for >2GB 🔗\n\n"
        "Open /menu to begin."
    )

def text_help():
    return (
        "❓ *How to use*\n"
        "1) Copy a public TeraBox share link (e.g. `https://terabox.com/s/...`).\n"
        "2) Send it here. If the file ≤ limit, I’ll download & send it.\n"
        "3) If it’s larger than the Telegram cap, I’ll give you a direct link.\n\n"
        "🔑 *Setup required (owner)*\n"
        "Set `TERABOX_COOKIE` in `.env` (format: `lang=...; ndus=...;`).\n"
        "This helps reliably extract direct links from TeraBox.\n\n"
        "🧹 *Clean VPS*\n"
        "I store files only in a temp folder and auto-delete after sending.\n\n"
        "💡 Tips: Works best for single files; folder links usually supply one file at a time."
    )

def text_limits(max_mb: int, user_rate: int, concurrent: int):
    return (
        "📏 *Limits & Quotas*\n"
        f"• Max upload size: `{max_mb} MB` (files above that are returned as direct links)\n"
        f"• Per-user downloads: `{user_rate}` per hour\n"
        f"• Concurrent downloads: `{concurrent}`\n"
        "• Only TeraBox domains are accepted."
    )

def text_privacy():
    return (
        "🔒 *Privacy & Terms*\n"
        "• Downloads are user-initiated; I do not keep logs of file contents.\n"
        "• Temp files are deleted immediately after sending.\n"
        "• Use only for content you have rights to. No bypassing paywalls or private links.\n"
        "• This bot shares your file with *you* in Telegram; be mindful of size limits."
    )
