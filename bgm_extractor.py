"""
╔══════════════════════════════════════════════════════════════╗
║         ZenixMusic — BGM Extractor Module                   ║
║  File    : bgm_extractor.py                                 ║
║  Requires: yt-dlp, ffmpeg (pkg install ffmpeg in Termux)    ║
║  Usage   : from bgm_extractor import register_bgm_handlers  ║
║            register_bgm_handlers(app)   ← main bot file mein║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import os
import re
from typing import Optional

import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

DOWNLOAD_DIR = "./bgm_temp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")

# Conversation states
_ASK_DURATION = 1

# Supported URL pattern
_URL_RE = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/watch|youtu\.be/|instagram\.com/|"
    r"facebook\.com/|twitter\.com/|x\.com/|tiktok\.com/|"
    r"soundcloud\.com/).+"
)

# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def _is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def _esc(t: str) -> str:
    """MarkdownV2 escape."""
    for c in r"\_*[]()~`>#+-=|{}.!":
        t = t.replace(c, f"\\{c}")
    return t


def _fmt(secs: float) -> str:
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_time(t: str) -> float:
    """'1:30' ya '90' → seconds (float)."""
    t = t.strip()
    if ":" in t:
        parts = t.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return int(parts[0]) * 60 + float(parts[1])
    return float(t)


def _ydl_opts(extra: dict | None = None) -> dict:
    base = {
        "format":      "bestaudio[ext=m4a]/bestaudio/best",
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
    }
    if os.path.isfile(COOKIES_FILE):
        base["cookiefile"] = COOKIES_FILE
    if extra:
        base.update(extra)
    return base


def _best_thumbnail(info: dict) -> str:
    thumbs = info.get("thumbnails") or []
    valid  = [
        (t.get("width", 0) * t.get("height", 0), t["url"])
        for t in thumbs if t.get("url", "").startswith("http")
    ]
    return max(valid, key=lambda x: x[0])[1] if valid else info.get("thumbnail", "")


def _get_video_info(url: str) -> Optional[dict]:
    """Video title, duration, thumbnail fetch (no download)."""
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title":     info.get("title", "Unknown"),
                "duration":  info.get("duration", 0),
                "thumbnail": _best_thumbnail(info),
                "uploader":  info.get("uploader", "Unknown"),
            }
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════

def _duration_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵  Full Audio",        callback_data="bgm_full")],
        [InlineKeyboardButton("✂️  Custom Duration",   callback_data="bgm_trim")],
        [InlineKeyboardButton("❌  Cancel",             callback_data="bgm_cancel")],
    ])

# ══════════════════════════════════════════════════════════════
#  DOWNLOAD + SEND
# ══════════════════════════════════════════════════════════════

async def _download_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    start_time: Optional[float],
    end_time: Optional[float],
) -> None:
    msg     = update.callback_query.message if update.callback_query else update.message
    chat_id = msg.chat_id
    uid     = update.effective_user.id

    out_full    = os.path.join(DOWNLOAD_DIR, f"bgm_{chat_id}_{uid}.mp3")
    out_trimmed = os.path.join(DOWNLOAD_DIR, f"bgm_{chat_id}_{uid}_trim.mp3")

    # ── Step 1: yt-dlp download ──────────────────────────────
    ydl_cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "-o", out_full,
        url,
    ]
    if os.path.isfile(COOKIES_FILE):
        ydl_cmd += ["--cookies", COOKIES_FILE]

    proc = await asyncio.create_subprocess_exec(
        *ydl_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode()[:300]
        await context.bot.send_message(
            chat_id,
            f"❌  Download failed\\!\n\n`{_esc(err)}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # ── Step 2: ffmpeg trim (optional) ───────────────────────
    final_file = out_full
    if start_time is not None and end_time is not None:
        duration = end_time - start_time
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i",   out_full,
            "-ss",  str(start_time),
            "-t",   str(duration),
            "-acodec", "copy",
            out_trimmed,
        ]
        trim_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await trim_proc.communicate()

        if trim_proc.returncode == 0 and os.path.isfile(out_trimmed):
            final_file = out_trimmed
        else:
            await context.bot.send_message(
                chat_id,
                "⚠️  Trim failed, sending full audio instead\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    # ── Step 3: size check ───────────────────────────────────
    if not os.path.isfile(final_file):
        await context.bot.send_message(chat_id, "❌  Audio file not found after download\\.", parse_mode=ParseMode.MARKDOWN_V2)
        _cleanup(out_full, out_trimmed)
        return

    size_mb = os.path.getsize(final_file) / (1024 * 1024)
    if size_mb > 50:
        await context.bot.send_message(
            chat_id,
            f"❌  File too large \\({size_mb:.1f} MB\\)\\.\n"
            f"Telegram allows max 50 MB\\. Try a shorter duration\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        _cleanup(out_full, out_trimmed)
        return

    # ── Step 4: Send ─────────────────────────────────────────
    info      = context.user_data.get("bgm_info", {})
    title     = info.get("title", "BGM")
    uploader  = info.get("uploader", "Unknown")
    dur_str   = (
        f"{_fmt(start_time)} → {_fmt(end_time)}"
        if start_time is not None else _fmt(info.get("duration", 0))
    )
    caption = (
        f"🎵  *{_esc(title)}*\n"
        f"👤  {_esc(uploader)}\n"
        f"⏱  `{dur_str}`\n\n"
        f"_Extracted by ZenixMusic_"
    )

    with open(final_file, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=f,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            filename=f"{title[:40]}.mp3",
            thumbnail=info.get("thumbnail") or None,
        )

    _cleanup(out_full, out_trimmed)
    context.user_data.pop("bgm_url", None)
    context.user_data.pop("bgm_info", None)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════
#  CONVERSATION HANDLERS
# ══════════════════════════════════════════════════════════════

async def bgm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/bgm command — accept URL as argument or ask for it."""
    args = context.args or []
    url  = args[0] if args else ""

    if url and _is_url(url):
        return await _process_url(update, context, url)

    await update.message.reply_text(
        "🎬  *BGM Extractor*\n\n"
        "Video ka link bhejo\\. Supported platforms:\n"
        "• YouTube  • Instagram  • TikTok\n"
        "• Facebook  • Twitter/X  • SoundCloud\n\n"
        "_Link paste karo 👇_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return _ASK_DURATION


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User ne URL bheja."""
    url = update.message.text.strip()
    if not _is_url(url):
        await update.message.reply_text(
            "❌  Valid video URL nahi hai\\. Dobara try karo\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION
    return await _process_url(update, context, url)


async def _process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> int:
    """URL validate, info fetch, duration choice dikhao."""
    msg = await update.message.reply_text(
        "🔍  Fetching video info\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _get_video_info, url)

    if not info:
        await msg.edit_text(
            "❌  Could not fetch video info\\.\n"
            "Check URL or try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION

    context.user_data["bgm_url"]  = url
    context.user_data["bgm_info"] = info

    thumb = info.get("thumbnail", "")
    text  = (
        f"🎬  *{_esc(info['title'][:60])}*\n"
        f"👤  {_esc(info['uploader'])}\n"
        f"⏱  `{_fmt(info['duration'])}`\n\n"
        f"⚙️  Kaisa audio chahiye?"
    )
    kb = _duration_kb()

    try:
        if thumb:
            await msg.delete()
            await update.message.reply_photo(
                photo=thumb,
                caption=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb,
            )
        else:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
    except Exception:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

    return _ASK_DURATION


async def handle_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Full audio download."""
    q = update.callback_query
    await q.answer()
    await q.edit_message_caption(
        caption="⏳  Downloading full audio\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    ) if q.message.caption else await q.edit_message_text(
        "⏳  Downloading full audio\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    url = context.user_data.get("bgm_url")
    await _download_and_send(update, context, url, None, None)
    return ConversationHandler.END


async def handle_trim_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Custom duration select kiya — time maango."""
    q = update.callback_query
    await q.answer()

    info = context.user_data.get("bgm_info", {})
    total = _fmt(info.get("duration", 0))

    text = (
        f"✂️  *Custom Duration*\n\n"
        f"Total duration: `{total}`\n\n"
        f"Format: `START\\-END`\n\n"
        f"*Examples:*\n"
        f"• `0:30\\-1:45` → 30 sec se 1 min 45 sec\n"
        f"• `10\\-90` → 10 sec se 90 sec\n"
        f"• `1:00\\-2:30` → 1 min se 2 min 30 sec\n\n"
        f"_Duration bhejo 👇_"
    )
    try:
        await q.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    return _ASK_DURATION


async def receive_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User ne duration bheja, parse karke download karo."""
    text = update.message.text.strip()

    # Agar naya URL aa gaya
    if _is_url(text):
        return await _process_url(update, context, text)

    # Duration parse
    if "-" not in text:
        await update.message.reply_text(
            "❌  Format galat hai\\!\n\nExample: `0:30\\-1:45` ya `30\\-105`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION

    try:
        parts = text.split("-", 1)
        start = _parse_time(parts[0])
        end   = _parse_time(parts[1])
    except Exception:
        await update.message.reply_text(
            "❌  Time parse nahi ho saka\\.\n\nExample: `1:00\\-2:30`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION

    if end <= start:
        await update.message.reply_text(
            "❌  End time, start time se zyada hona chahiye\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION

    info  = context.user_data.get("bgm_info", {})
    total = info.get("duration", 0)
    if total and end > total:
        await update.message.reply_text(
            f"❌  End time `{_fmt(end)}` video duration `{_fmt(total)}` se zyada hai\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return _ASK_DURATION

    await update.message.reply_text(
        f"⏳  Downloading & trimming "
        f"`{_fmt(start)}` → `{_fmt(end)}` "
        f"\\({_fmt(end - start)} duration\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    url = context.user_data.get("bgm_url")
    await _download_and_send(update, context, url, start, end)
    return ConversationHandler.END


async def bgm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel button."""
    q = update.callback_query
    await q.answer("Cancelled")
    try:
        await q.message.delete()
    except Exception:
        pass
    context.user_data.pop("bgm_url", None)
    context.user_data.pop("bgm_info", None)
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel command."""
    context.user_data.pop("bgm_url", None)
    context.user_data.pop("bgm_info", None)
    await update.message.reply_text("❌  BGM extraction cancelled\\.", parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  REGISTER FUNCTION  ← main bot mein sirf yeh call karo
# ══════════════════════════════════════════════════════════════

def register_bgm_handlers(app: Application) -> None:
    """
    ZenixMusic main.py mein:
        from bgm_extractor import register_bgm_handlers
        register_bgm_handlers(app)
    """
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("bgm", bgm_cmd),
            # Sirf conversation ke baad URL messages handle karo
        ],
        states={
            _ASK_DURATION: [
                # Buttons
                CallbackQueryHandler(handle_full,         pattern="^bgm_full$"),
                CallbackQueryHandler(handle_trim_prompt,  pattern="^bgm_trim$"),
                CallbackQueryHandler(bgm_cancel,          pattern="^bgm_cancel$"),
                # Text input (URL ya duration)
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_duration),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
        ],
        per_chat=True,
        per_user=True,
        conversation_timeout=300,  # 5 min mein auto-expire
    )

    app.add_handler(conv)
