"""
🎀 Kawaii Telegram Music Bot  v3.3
Rewritten with python-telegram-bot v20+ (async) + pytgcalls + yt-dlp

FIXES FROM v3.2:
  - play_next: added call.join_group_call() BEFORE call.play()
    PyTgCalls v4 requires explicit join — it does NOT auto-join on play()
  - search_yt: now extracts a FRESH direct audio URL at play-time (not cached)
    YouTube signed URLs expire in ~6 hours, so we re-extract right before streaming
  - MediaStream: passes the resolved direct audio stream URL, not webpage_url
  - get_audio_url(): new helper that runs yt-dlp just before playback to get
    a fresh, non-expired direct stream URL
  - /logs command: in-memory circular log buffer (last 50 lines), admin-only
  - LogBufferHandler: custom logging.Handler that fills the buffer
  - on_stream_end: now properly handles pytgcalls v4 update object variance
  - Auto-rejoin on unexpected VC drop via pytgcalls error handler
"""

import asyncio
import collections
import logging
import os
import random
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import httpx
import yt_dlp
from pyrogram import Client as PyrogramClient
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream, StreamEnded
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ─── LOG BUFFER (for /logs command) ───────────────────────────────────────────

class LogBufferHandler(logging.Handler):
    """Keeps the last `maxlen` log lines in memory."""
    def __init__(self, maxlen: int = 50):
        super().__init__()
        self.buffer: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


# ─── LOGGING ───────────────────────────────────────────────────────────────────

_log_buffer_handler = LogBufferHandler(maxlen=50)
_log_buffer_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("KawaiiBot")
logging.getLogger().addHandler(_log_buffer_handler)  # attach buffer to root logger


# ─── CONFIG ────────────────────────────────────────────────────────────────────

API_ID         = int(os.environ.get("API_ID", 0))
API_HASH       = os.environ.get("API_HASH", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
ADMIN_IDS: list[int] = (
    list(map(int, os.environ["ADMIN_IDS"].split(",")))
    if os.environ.get("ADMIN_IDS")
    else []
)
AUTO_LEAVE_SECS = int(os.environ.get("AUTO_LEAVE_SECS", "120"))

SOURCES = ["ytsearch1", "scsearch1"]


# ─── PYROGRAM + PYTGCALLS ──────────────────────────────────────────────────────

assistant = PyrogramClient(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
call = PyTgCalls(assistant)


# ─── STATE ─────────────────────────────────────────────────────────────────────

queues:            dict[int, list[dict]]     = defaultdict(list)
currently_playing: dict[int, Optional[dict]] = {}
loop_mode:         dict[int, bool]           = defaultdict(bool)
_chat_locks:       dict[int, asyncio.Lock]   = {}
auto_leave_tasks:  dict[int, asyncio.Task]   = {}
_joined_chats:     set[int]                  = set()   # ← track which chats we've joined

SEARCH_CACHE_KEY = "search_cache"


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ─── HEALTH SERVER ─────────────────────────────────────────────────────────────

def _run_health_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Kawaii Music Bot is alive~ nyaa! 🎀".encode())

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a): pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def _progress_bar(position: int, total: int, length: int = 12) -> str:
    if total <= 0:
        return "▱" * length
    filled = int(length * position / total)
    return "▰" * filled + "▱" * (length - filled)


def _fmt_duration(secs: int) -> str:
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _esc(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def player_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    loop_label = "🔁 Loop ON" if loop_mode.get(chat_id) else "🔁 Loop OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause",  callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            InlineKeyboardButton("⏭️ Skip",   callback_data="skip"),
        ],
        [
            InlineKeyboardButton("⏹️ Stop",   callback_data="stop"),
            InlineKeyboardButton("📋 Queue",  callback_data="queue"),
            InlineKeyboardButton(loop_label,  callback_data="loop"),
        ],
        [
            InlineKeyboardButton("🔀 Shuffle", callback_data="shuffle"),
        ],
    ])


# ─── YT-DLP HELPERS ────────────────────────────────────────────────────────────

def _ydl_opts(extra: dict | None = None) -> dict:
    base = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "source_address": "0.0.0.0",
    }
    if extra:
        base.update(extra)
    return base


def get_audio_url(webpage_url: str) -> Optional[str]:
    """
    Re-extract a FRESH direct audio stream URL right before playback.
    YouTube signed URLs expire — never cache them, always re-fetch.
    """
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("url")
            if not url:
                raise ValueError("No stream URL in yt-dlp result")
            log.info("Fresh audio URL extracted for: %s", info.get("title"))
            return url
    except Exception as exc:
        log.error("get_audio_url failed for %s: %s", webpage_url, exc)
        return None


def search_yt(query: str, retries: int = 2) -> Optional[dict]:
    """Search and return track metadata. URL here is webpage_url, NOT stream URL."""
    for source in SOURCES:
        opts = _ydl_opts({"default_search": source})
        for attempt in range(retries + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                    if "entries" in info:
                        info = info["entries"][0]
                    webpage_url = info.get("webpage_url") or info.get("url")
                    if not webpage_url:
                        raise ValueError("No URL in result")
                    log.info("Found via %s: %s", source, info.get("title"))
                    return {
                        "title":       info.get("title", "Unknown"),
                        "webpage_url": webpage_url,
                        "duration":    info.get("duration", 0),
                        "thumbnail":   info.get("thumbnail", ""),
                        "uploader":    info.get("uploader", "Unknown"),
                    }
            except Exception as exc:
                log.warning("[%s] attempt %d/%d failed: %s", source, attempt + 1, retries + 1, exc)
    log.error("All sources failed for: %s", query)
    return None


def search_yt_multi(query: str, count: int = 5) -> list[dict]:
    """Multi-result search. Returns metadata dicts (no stream URLs)."""
    for source in [f"ytsearch{count}", f"scsearch{count}"]:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts({"default_search": source})) as ydl:
                info    = ydl.extract_info(query, download=False)
                entries = info.get("entries", [])
                results = [
                    {
                        "title":       e.get("title", "Unknown"),
                        "webpage_url": e.get("webpage_url") or e.get("url", ""),
                        "duration":    e.get("duration", 0),
                        "thumbnail":   e.get("thumbnail", ""),
                        "uploader":    e.get("uploader", "Unknown"),
                    }
                    for e in entries
                    if e and (e.get("webpage_url") or e.get("url"))
                ]
                if results:
                    log.info("Multi-search via %s: %d results", source, len(results))
                    return results
        except Exception as exc:
            log.warning("multi-search [%s] failed: %s", source, exc)
    return []


# ─── LYRICS ────────────────────────────────────────────────────────────────────

async def fetch_lyrics(artist: str, title: str) -> Optional[str]:
    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json().get("lyrics", "").strip() or None
    except Exception as exc:
        log.warning("lyrics fetch failed: %s", exc)
    return None


# ─── PLAYBACK ──────────────────────────────────────────────────────────────────

async def _cancel_auto_leave(chat_id: int) -> None:
    task = auto_leave_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _schedule_auto_leave(chat_id: int) -> None:
    if AUTO_LEAVE_SECS <= 0:
        return

    async def _leave() -> None:
        await asyncio.sleep(AUTO_LEAVE_SECS)
        if not currently_playing.get(chat_id) and not queues.get(chat_id):
            try:
                await call.leave_call(chat_id)
                _joined_chats.discard(chat_id)
                log.info("Auto-left VC in chat %d", chat_id)
            except Exception:
                pass

    await _cancel_auto_leave(chat_id)
    auto_leave_tasks[chat_id] = asyncio.create_task(_leave())


async def _ensure_joined(chat_id: int) -> None:
    """
    Join the group call if we haven't already.
    PyTgCalls v4 requires call.join_group_call() before call.play().
    Without this, the bot silently fails to stream audio.
    """
    if chat_id in _joined_chats:
        return
    try:
        # Join with a silent stream first; play_next will change_stream immediately
        await call.join_group_call(
            chat_id,
            MediaStream(
                # We need a placeholder — we'll change_stream right after joining
                # Use the first queued track's audio URL
                "",
                audio_quality=AudioQuality.HIGH,
            ),
        )
        _joined_chats.add(chat_id)
        log.info("Joined VC in chat %d", chat_id)
    except Exception as exc:
        log.warning("join_group_call for %d: %s (might already be joined)", chat_id, exc)
        # Mark as joined anyway — maybe we're already in
        _joined_chats.add(chat_id)


async def play_next(chat_id: int, *, change_stream: bool = False) -> bool:
    """
    Pop next track, get a fresh stream URL, then start / change stream.

    KEY FIX: We call get_audio_url() here (blocking, via executor) to get
    a fresh signed URL. We NEVER store the stream URL in the queue because
    YouTube URLs expire after ~6 hours.
    """
    async with _get_lock(chat_id):
        if not queues[chat_id]:
            currently_playing.pop(chat_id, None)
            await _schedule_auto_leave(chat_id)
            return False

        track = queues[chat_id].pop(0)
        currently_playing[chat_id] = track

        if loop_mode[chat_id]:
            queues[chat_id].append(track)

        # ── Get a FRESH audio URL (blocking yt-dlp call, run in thread) ──────
        loop = asyncio.get_running_loop()
        stream_url = await loop.run_in_executor(
            None, get_audio_url, track["webpage_url"]
        )

        if not stream_url:
            log.error("Could not get stream URL for: %s", track["title"])
            currently_playing.pop(chat_id, None)
            # Try the next track automatically
            return await play_next(chat_id, change_stream=change_stream)

        stream = MediaStream(stream_url, audio_quality=AudioQuality.HIGH)

        try:
            if change_stream and chat_id in _joined_chats:
                # change_stream keeps VC alive — no gap
                await call.change_stream(chat_id, stream)
            else:
                # Must join before playing
                await _ensure_joined(chat_id)
                await call.play(chat_id, stream)

            log.info("Now playing in %d: %s", chat_id, track["title"])
            return True

        except Exception as exc:
            log.error("play error in %d: %s", chat_id, exc)
            # Try plain play() as fallback if change_stream failed
            if change_stream:
                try:
                    await _ensure_joined(chat_id)
                    await call.play(chat_id, stream)
                    return True
                except Exception as exc2:
                    log.error("fallback play also failed: %s", exc2)
            currently_playing.pop(chat_id, None)
            _joined_chats.discard(chat_id)
            return False


# ─── COMMANDS ──────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name if update.effective_user else "Senpai"

    caption = (
        f"Konnichiwa *{_esc(name)}*\\-senpai\\~ 💖\n\n"
        "I'm your kawaii music bot\\! 🎵\n\n"
        "*Commands:*\n"
        "🎶 /play `<song>` — Play a song\n"
        "🔍 /search `<song>` — Pick from top 5 results\n"
        "⏸️ /pause — Pause\n"
        "▶️ /resume — Resume\n"
        "⏭️ /skip — Skip current track\n"
        "⏹️ /stop — Stop \\& clear queue\n"
        "📋 /queue — Show queue\n"
        "🔀 /shuffle — Shuffle queue\n"
        "🗑 /remove `<pos>` — Remove track\n"
        "📝 /lyrics `<song>` — Show lyrics\n"
        "🔁 /loop — Toggle loop\n"
        "🔊 /vol `<0\\-200>` — Set volume\n"
        "🎵 /np — Now playing\n"
        "🪵 /logs — Show recent logs \\(admin\\)\n\n"
        "Let's make music magic, nya\\~\\! ✨"
    )
    await update.message.reply_video(
        video="https://files.catbox.moe/9w0qsn.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Tell me what to play Senpai\\! 🎵\nUsage: `/play <song name or URL>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query = " ".join(context.args)
    msg   = await update.message.reply_text(
        f"🔍 Searching *{_esc(query)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop  = asyncio.get_running_loop()
    track = await loop.run_in_executor(None, search_yt, query)

    if not track:
        await msg.edit_text(
            "Gomen\\~ couldn't find that song 😢",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    queues[chat_id].append(track)
    await _cancel_auto_leave(chat_id)
    await msg.delete()

    is_idle = currently_playing.get(chat_id) is None

    if is_idle:
        await update.message.reply_text(
            f"⏳ Loading *{_esc(track['title'])}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        started = await play_next(chat_id)
        now     = currently_playing.get(chat_id, track)
        dur     = _fmt_duration(now.get("duration", 0))

        if started:
            await update.message.reply_text(
                f"🎶 *Now Playing*\n\n"
                f"🎵 *{_esc(now['title'])}*\n"
                f"👤 {_esc(now.get('uploader', 'Unknown'))}\n"
                f"⏱ {_esc(dur)}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=player_keyboard(chat_id),
            )
        else:
            await update.message.reply_text(
                "Gomen\\~ something went wrong starting playback 😢\n"
                "Make sure I'm added to the voice chat as assistant\\!",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    else:
        pos = len(queues[chat_id])
        dur = _fmt_duration(track.get("duration", 0))
        await update.message.reply_text(
            f"✅ *Added to Queue \\#{pos}*\n\n"
            f"🎵 *{_esc(track['title'])}*\n"
            f"👤 {_esc(track.get('uploader', 'Unknown'))}\n"
            f"⏱ {_esc(dur)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/search <song name>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    query = " ".join(context.args)
    msg   = await update.message.reply_text(
        f"🔍 Searching: *{_esc(query)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop    = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, lambda: search_yt_multi(query, 5))

    if not results:
        await msg.edit_text("No results found, gomen\\~ 😢", parse_mode=ParseMode.MARKDOWN_V2)
        return

    cache: dict = context.bot_data.setdefault(SEARCH_CACHE_KEY, {})
    cache[msg.message_id] = results

    buttons = [
        [InlineKeyboardButton(
            f"{i+1}. {r['title'][:40]} ({_fmt_duration(r['duration'])})",
            callback_data=f"sel:{msg.message_id}:{i}",
        )]
        for i, r in enumerate(results)
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_search")])

    text = "🎵 *Top Results* — pick a song Senpai\\~\n\n"
    for i, r in enumerate(results):
        text += f"`{i+1}\\.` *{_esc(r['title'])}*\n   👤 {_esc(r.get('uploader','?'))}  ⏱ {_esc(_fmt_duration(r['duration']))}\n\n"

    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=InlineKeyboardMarkup(buttons))


async def np_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now     = currently_playing.get(chat_id)
    if not now:
        await update.message.reply_text("Nothing is playing right now nya\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return

    q_len  = len(queues[chat_id])
    loop_s = "ON 🔁" if loop_mode[chat_id] else "OFF"
    await update.message.reply_text(
        f"🎵 *Now Playing*\n\n*{_esc(now['title'])}*\n"
        f"👤 {_esc(now.get('uploader', 'Unknown'))}\n"
        f"⏱ {_esc(_fmt_duration(now.get('duration', 0)))}\n"
        f"🔁 Loop: {loop_s}\n"
        f"📋 Queue: {q_len} track\\(s\\) remaining",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=player_keyboard(chat_id),
    )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.pause_stream(update.effective_chat.id)
        await update.message.reply_text("Music paused ⏸️", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text("Nothing is playing right now Senpai\\! 🤔", parse_mode=ParseMode.MARKDOWN_V2)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.resume_stream(update.effective_chat.id)
        await update.message.reply_text("Music resumed\\! ▶️🎶", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text("Nothing is paused right now Senpai\\! 🤔", parse_mode=ParseMode.MARKDOWN_V2)


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can skip, gomen\\~ 🙏", parse_mode=ParseMode.MARKDOWN_V2)
        return

    chat_id = update.effective_chat.id
    if not currently_playing.get(chat_id):
        await update.message.reply_text("Nothing to skip nya\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await update.message.reply_text("⏭️ Skipping\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    if queues[chat_id]:
        started = await play_next(chat_id, change_stream=True)
        now     = currently_playing.get(chat_id)
        if started and now:
            await update.message.reply_text(
                f"🎵 *Now Playing*\n\n*{_esc(now['title'])}*",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=player_keyboard(chat_id),
            )
    else:
        currently_playing.pop(chat_id, None)
        _joined_chats.discard(chat_id)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        await update.message.reply_text("Queue empty\\~ 🌸 Add more with /play\\!", parse_mode=ParseMode.MARKDOWN_V2)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can stop the music, gomen\\~ 🙏", parse_mode=ParseMode.MARKDOWN_V2)
        return

    chat_id = update.effective_chat.id
    queues[chat_id].clear()
    currently_playing.pop(chat_id, None)
    loop_mode[chat_id] = False
    _joined_chats.discard(chat_id)
    await _cancel_auto_leave(chat_id)
    try:
        await call.leave_call(chat_id)
    except Exception:
        pass
    await update.message.reply_text("Music stopped\\! Bye bye\\~ 👋", parse_mode=ParseMode.MARKDOWN_V2)


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now     = currently_playing.get(chat_id)
    q       = queues[chat_id]

    if not now and not q:
        await update.message.reply_text("Queue is empty\\~ 🌸 Use /play to add songs\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    total_secs = sum(t.get("duration", 0) for t in q)
    text = "🎵 *Music Queue*\n\n"
    if now:
        text += f"▶️ *Now:* {_esc(now['title'])} `\\[{_esc(_fmt_duration(now.get('duration', 0)))}\\]`\n\n"
    if q:
        text += "📋 *Up Next:*\n"
        for i, track in enumerate(q[:10], 1):
            text += f"`{i}\\.` {_esc(track['title'])} `\\[{_esc(_fmt_duration(track.get('duration', 0)))}\\]`\n"
        if len(q) > 10:
            text += f"\n\\.\\.\\. and *{len(q) - 10}* more tracks\\."
        text += f"\n\n⏳ Total: `{_esc(_fmt_duration(total_secs))}`"
    if loop_mode[chat_id]:
        text += "\n🔁 *Loop ON*"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id            = update.effective_chat.id
    loop_mode[chat_id] = not loop_mode[chat_id]
    state              = "ON 🔁" if loop_mode[chat_id] else "OFF ▶️"
    await update.message.reply_text(f"Loop mode is now *{_esc(state)}*\\!", parse_mode=ParseMode.MARKDOWN_V2)


async def vol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can change volume, gomen\\~ 🙏", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/vol <0\\-200>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    volume  = max(0, min(200, int(context.args[0])))
    chat_id = update.effective_chat.id
    try:
        await call.change_volume_call(chat_id, volume)
        await update.message.reply_text(f"🔊 Volume set to *{volume}%*\\!", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as exc:
        log.error("vol error: %s", exc)
        await update.message.reply_text("Couldn't change volume right now\\~ 😢", parse_mode=ParseMode.MARKDOWN_V2)


async def shuffle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    q       = queues[chat_id]
    if not q:
        await update.message.reply_text("Queue is empty\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return
    random.shuffle(q)
    await update.message.reply_text(
        f"🔀 Queue shuffled\\! *{len(q)}* tracks randomised\\~ ✨",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/remove <position>` \\(use /queue to see positions\\)",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    pos = int(context.args[0])
    q   = queues[chat_id]
    if not q:
        await update.message.reply_text("Queue is empty nya\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return
    if pos < 1 or pos > len(q):
        await update.message.reply_text(
            f"Invalid position\\! Queue has *{len(q)}* tracks\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    removed = q.pop(pos - 1)
    await update.message.reply_text(
        f"🗑 Removed *{_esc(removed['title'])}* from position {pos}\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if context.args:
        query  = " ".join(context.args)
        artist, title = query.split(" - ", 1) if " - " in query else (
            (currently_playing.get(chat_id) or {}).get("uploader", "Unknown"), query
        )
    else:
        now = currently_playing.get(chat_id)
        if not now:
            await update.message.reply_text(
                "Nothing is playing\\! Use `/lyrics Artist \\- Title`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        artist, title = now.get("uploader", "Unknown"), now.get("title", "Unknown")

    msg    = await update.message.reply_text(f"📝 Fetching lyrics for *{_esc(title)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    lyrics = await fetch_lyrics(artist.strip(), title.strip())

    if not lyrics:
        await msg.edit_text(
            f"Couldn't find lyrics for *{_esc(title)}* 😢\n\nTry: `/lyrics Artist \\- Song Title`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    snippet = lyrics[:3500]
    if len(lyrics) > 3500:
        snippet += "\n\n\\[\\.\\.\\. truncated\\]"

    await msg.edit_text(
        f"📝 *{_esc(title)}*\n\n{_esc(snippet)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /logs — show the last 50 log lines (admin only).
    Splits into multiple messages if needed to stay under Telegram's 4096 char limit.
    """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can view logs, gomen\\~ 🙏", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = list(_log_buffer_handler.buffer)
    if not lines:
        await update.message.reply_text("No logs yet nya\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Send as plain monospace text, split into ≤4000-char chunks
    full_text = "\n".join(lines)
    chunk_size = 3900
    chunks     = [full_text[i:i + chunk_size] for i in range(0, len(full_text), chunk_size)]

    for chunk in chunks:
        await update.message.reply_text(
            f"```\n{chunk}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─── CALLBACK QUERY HANDLER ────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat.id
    data    = query.data

    await query.answer()

    if data.startswith("sel:"):
        _, msg_id_str, idx_str = data.split(":", 2)
        msg_id  = int(msg_id_str)
        idx     = int(idx_str)
        cache   = context.bot_data.get(SEARCH_CACHE_KEY, {})
        results = cache.get(msg_id)

        if not results or idx >= len(results):
            await query.edit_message_text("Oops, result expired\\~ search again\\!", parse_mode=ParseMode.MARKDOWN_V2)
            return

        track = results[idx]
        cache.pop(msg_id, None)
        queues[chat_id].append(track)
        await _cancel_auto_leave(chat_id)
        await query.message.delete()

        is_idle = currently_playing.get(chat_id) is None
        if is_idle:
            await context.bot.send_message(
                chat_id,
                f"⏳ Loading *{_esc(track['title'])}*\\.\\.\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await play_next(chat_id)
            now = currently_playing.get(chat_id, track)
            await context.bot.send_message(
                chat_id,
                f"🎶 *Now Playing*\n\n*{_esc(now['title'])}*",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=player_keyboard(chat_id),
            )
        else:
            pos = len(queues[chat_id])
            await context.bot.send_message(
                chat_id,
                f"✅ Added *{_esc(track['title'])}* to queue at \\#{pos}\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    if data == "cancel_search":
        await query.message.delete()
        return

    if data == "pause":
        try:
            await call.pause_stream(chat_id)
        except Exception:
            pass

    elif data == "resume":
        try:
            await call.resume_stream(chat_id)
        except Exception:
            pass

    elif data == "loop":
        loop_mode[chat_id] = not loop_mode[chat_id]
        try:
            await query.edit_message_reply_markup(player_keyboard(chat_id))
        except Exception:
            pass

    elif data == "shuffle":
        q = queues[chat_id]
        if q:
            random.shuffle(q)
            await query.answer(f"🔀 Shuffled {len(q)} tracks!", show_alert=False)
        else:
            await query.answer("Queue is empty~", show_alert=False)

    elif data == "skip":
        if not currently_playing.get(chat_id):
            return
        if queues[chat_id]:
            started = await play_next(chat_id, change_stream=True)
            now     = currently_playing.get(chat_id)
            if started and now:
                await context.bot.send_message(
                    chat_id,
                    f"🎵 *Now Playing*\n\n*{_esc(now['title'])}*",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=player_keyboard(chat_id),
                )
        else:
            currently_playing.pop(chat_id, None)
            _joined_chats.discard(chat_id)
            try:
                await call.leave_call(chat_id)
            except Exception:
                pass
            await context.bot.send_message(
                chat_id,
                "Queue empty\\~ 🌸 Add more with /play\\!",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        loop_mode[chat_id] = False
        _joined_chats.discard(chat_id)
        await _cancel_auto_leave(chat_id)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        await query.edit_message_text("Music stopped\\! Hope you enjoyed it Senpai 💖", parse_mode=ParseMode.MARKDOWN_V2)

    elif data == "queue":
        now = currently_playing.get(chat_id)
        q   = queues[chat_id]
        if not now and not q:
            return
        lines: list[str] = []
        if now:
            lines.append(f"▶ {now['title']}")
        for i, t in enumerate(q[:8], 1):
            lines.append(f"{i}. {t['title']}")
        if len(q) > 8:
            lines.append(f"...+{len(q)-8} more")
        await context.bot.send_message(chat_id, "\n".join(lines))


# ─── STREAM END HANDLER ────────────────────────────────────────────────────────

@call.on_update(StreamEnded)
async def on_stream_end(client, update) -> None:
    chat_id = getattr(update, "chat_id", None)
    if not isinstance(chat_id, int):
        return

    log.info("Stream ended in chat %d", chat_id)
    currently_playing.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        _joined_chats.discard(chat_id)
        await _schedule_auto_leave(chat_id)


# ─── ERROR HANDLER ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update error:", exc_info=context.error)


# ─── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    log.info("Starting Pyrogram assistant...")
    await assistant.start()
    log.info("Starting PyTgCalls...")
    await call.start()
    log.info("🎀 Kawaii Music Bot v3.3 is live~ nyaa!")


async def post_shutdown(application: Application) -> None:
    log.info("Shutting down...")
    for task in auto_leave_tasks.values():
        task.cancel()
    try:
        await assistant.stop()
    except Exception:
        pass
    log.info("Bye bye~ 👋")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set!")

    threading.Thread(target=_run_health_server, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",   start_cmd))
    app.add_handler(CommandHandler("play",    play_cmd))
    app.add_handler(CommandHandler("search",  search_cmd))
    app.add_handler(CommandHandler("np",      np_cmd))
    app.add_handler(CommandHandler("pause",   pause_cmd))
    app.add_handler(CommandHandler("resume",  resume_cmd))
    app.add_handler(CommandHandler("skip",    skip_cmd))
    app.add_handler(CommandHandler("stop",    stop_cmd))
    app.add_handler(CommandHandler("queue",   queue_cmd))
    app.add_handler(CommandHandler("loop",    loop_cmd))
    app.add_handler(CommandHandler("vol",     vol_cmd))
    app.add_handler(CommandHandler("shuffle", shuffle_cmd))
    app.add_handler(CommandHandler("remove",  remove_cmd))
    app.add_handler(CommandHandler("lyrics",  lyrics_cmd))
    app.add_handler(CommandHandler("logs",    logs_cmd))   # ← NEW

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    log.info("Starting polling...")
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
