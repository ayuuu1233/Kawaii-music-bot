"""
🎀 Kawaii Telegram Music Bot v3.9
Compatible with: pytgcalls==2.1.0 (MarshalX / GroupCallFactory API)

Key changes from v3.8:
  - PyTgCalls / MediaStream / StreamEnded → GroupCallFactory / GroupCallFile / on_playout_ended
  - Per-chat GroupCallFile instances in _group_calls dict
  - Audio streamed via ffmpeg → raw PCM s16le file → GroupCallFile
  - Stream end: on_playout_ended callback → queue advance
  - VC join: group_call.start(chat_id) instead of call.play()
  - play_on_repeat=False so playout_ended fires once per track
  - Thumbnail: _best_thumbnail() from thumbnails list (highest res)
"""

import asyncio
import collections
import logging
import os
import random
import subprocess
import tempfile
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import httpx
import yt_dlp
from pyrogram import Client as PyrogramClient
from pytgcalls import GroupCallFactory
from pytgcalls.implementation.group_call_file import GroupCallFile, GroupCallFileAction
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


# ─── LOG BUFFER ───────────────────────────────────────────────────────────────

class LogBufferHandler(logging.Handler):
    def __init__(self, maxlen: int = 50):
        super().__init__()
        self.buffer: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


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
logging.getLogger().addHandler(_log_buffer_handler)


# ─── CONFIG ───────────────────────────────────────────────────────────────────

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
COOKIES_FILE    = os.environ.get("COOKIES_FILE", "cookies.txt")
SOURCES         = ["ytsearch1", "scsearch1"]

# Temp dir for PCM audio files
AUDIO_TMP_DIR = tempfile.mkdtemp(prefix="kawaii_audio_")


# ─── PYROGRAM CLIENT ─────────────────────────────────────────────────────────

assistant = PyrogramClient(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)


# ─── STATE ────────────────────────────────────────────────────────────────────

_group_calls:      dict[int, GroupCallFile]     = {}   # per-chat GroupCallFile
queues:            dict[int, list[dict]]         = defaultdict(list)
currently_playing: dict[int, Optional[dict]]     = {}
loop_mode:         dict[int, bool]               = defaultdict(bool)
_chat_locks:       dict[int, asyncio.Lock]       = {}
auto_leave_tasks:  dict[int, asyncio.Task]       = {}

# Set in post_init so callbacks can access bot
_bot_app: Optional[Application] = None

SEARCH_CACHE_KEY = "search_cache"


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ─── HEALTH SERVER ────────────────────────────────────────────────────────────

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
        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def _fmt_duration(secs: int) -> str:
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def player_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    loop_label = "🔁 Loop ON" if loop_mode.get(chat_id) else "🔁 Loop OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause",   callback_data="pause"),
            InlineKeyboardButton("▶️ Resume",  callback_data="resume"),
            InlineKeyboardButton("⏭️ Skip",    callback_data="skip"),
        ],
        [
            InlineKeyboardButton("⏹️ Stop",    callback_data="stop"),
            InlineKeyboardButton("📋 Queue",   callback_data="queue"),
            InlineKeyboardButton(loop_label,   callback_data="loop"),
        ],
        [
            InlineKeyboardButton("🔀 Shuffle", callback_data="shuffle"),
        ],
    ])


async def _safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


async def send_now_playing(bot, chat_id: int, track: dict) -> None:
    dur = _fmt_duration(track.get("duration", 0))
    caption = (
        f"🎶 *Now Playing*\n\n"
        f"🎵 *{_esc(track['title'])}*\n"
        f"👤 {_esc(track.get('uploader', 'Unknown'))}\n"
        f"⏱ {_esc(dur)}"
    )
    thumbnail = track.get("thumbnail", "")
    sent = False
    if thumbnail:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=player_keyboard(chat_id),
            )
            sent = True
        except Exception as e:
            log.warning("Thumbnail send failed: %s", e)
    if not sent:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=player_keyboard(chat_id),
        )


# ─── YT-DLP ──────────────────────────────────────────────────────────────────

def _ydl_opts(extra: dict | None = None) -> dict:
    base = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "source_address": "0.0.0.0",
    }
    if os.path.isfile(COOKIES_FILE):
        base["cookiefile"] = COOKIES_FILE
    if extra:
        base.update(extra)
    return base


def _best_thumbnail(info: dict) -> str:
    """Pick highest resolution thumbnail from yt-dlp info dict."""
    thumbnails = info.get("thumbnails") or []
    valid = [
        (t.get("width", 0) * t.get("height", 0), t["url"])
        for t in thumbnails
        if t.get("url", "").startswith("http")
    ]
    if valid:
        return max(valid, key=lambda x: x[0])[1]
    return info.get("thumbnail", "")


def search_yt(query: str, retries: int = 2) -> Optional[dict]:
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
                        raise ValueError("No URL")
                    log.info("Found via %s: %s", source, info.get("title"))
                    return {
                        "title":       info.get("title", "Unknown"),
                        "webpage_url": webpage_url,
                        "duration":    info.get("duration", 0),
                        "thumbnail":   _best_thumbnail(info),
                        "uploader":    info.get("uploader", "Unknown"),
                    }
            except Exception as exc:
                log.warning("[%s] attempt %d: %s", source, attempt + 1, exc)
    return None


def search_yt_multi(query: str, count: int = 5) -> list[dict]:
    for source in [f"ytsearch{count}", f"scsearch{count}"]:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts({"default_search": source})) as ydl:
                info = ydl.extract_info(query, download=False)
                results = [
                    {
                        "title":       e.get("title", "Unknown"),
                        "webpage_url": e.get("webpage_url") or e.get("url", ""),
                        "duration":    e.get("duration", 0),
                        "thumbnail":   _best_thumbnail(e),
                        "uploader":    e.get("uploader", "Unknown"),
                    }
                    for e in info.get("entries", [])
                    if e and (e.get("webpage_url") or e.get("url"))
                ]
                if results:
                    return results
        except Exception as exc:
            log.warning("multi-search [%s]: %s", source, exc)
    return []


def get_stream_url(webpage_url: str) -> tuple[str, str]:
    """Returns (direct_audio_url, best_thumbnail). Empty strings on failure."""
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("url", "")
            if not url:
                raise ValueError("No stream URL")
            return url, _best_thumbnail(info)
    except Exception as exc:
        log.error("get_stream_url failed: %s", exc)
        return "", ""


def convert_to_pcm(stream_url: str, out_path: str) -> bool:
    """
    ffmpeg: streaming URL → raw PCM s16le 48000Hz stereo
    pytgcalls GroupCallFile needs exactly this format.
    """
    cmd = [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-f", "s16le",
        "-ar", "48000",
        "-ac", "2",
        "-acodec", "pcm_s16le",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr[-800:].decode(errors="replace")
            log.error("ffmpeg failed (code %d): %s", result.returncode, stderr_tail)
            return False
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        log.info("PCM ready: %s (%d KB)", out_path, size // 1024)
        return size > 0
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return False
    except FileNotFoundError:
        log.error("ffmpeg not found! Run: apt install ffmpeg")
        return False
    except Exception as exc:
        log.error("convert_to_pcm error: %s", exc)
        return False


# ─── GROUP CALL PER CHAT ─────────────────────────────────────────────────────

def _make_group_call(chat_id: int) -> GroupCallFile:
    """Create a new GroupCallFile for this chat and register playout_ended."""
    gc = GroupCallFactory(assistant).get_file_group_call(
        input_filename="",
        play_on_repeat=False,  # CRITICAL: fires playout_ended when file ends
    )

    @gc.on_playout_ended
    def _playout_ended(gc_inst, filename: str):
        log.info("Playout ended in chat %d | file: %s", chat_id, filename)
        # Schedule coroutine on the running event loop
        try:
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(_on_track_finished(chat_id), loop)
        except Exception as e:
            log.error("playout_ended scheduling error: %s", e)

    _group_calls[chat_id] = gc
    log.info("GroupCallFile created for chat %d", chat_id)
    return gc


def _get_or_make_gc(chat_id: int) -> GroupCallFile:
    if chat_id not in _group_calls:
        return _make_group_call(chat_id)
    return _group_calls[chat_id]


async def _on_track_finished(chat_id: int) -> None:
    """Advance queue after a track ends. Called from playout_ended callback."""
    # Clean up temp PCM file
    current = currently_playing.get(chat_id)
    if current:
        tmp = current.get("_tmp_file", "")
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        # Loop mode: re-add to queue
        if loop_mode[chat_id]:
            queues[chat_id].append({k: v for k, v in current.items() if k != "_tmp_file"})

    currently_playing.pop(chat_id, None)

    if queues.get(chat_id):
        started = await _play_next_locked(chat_id)
        if started and _bot_app:
            now = currently_playing.get(chat_id)
            if now:
                try:
                    await send_now_playing(_bot_app.bot, chat_id, now)
                except Exception as e:
                    log.error("send_now_playing error: %s", e)
        if not started:
            await _schedule_auto_leave(chat_id)
    else:
        await _schedule_auto_leave(chat_id)


# ─── AUTO LEAVE ───────────────────────────────────────────────────────────────

async def _cancel_auto_leave(chat_id: int) -> None:
    task = auto_leave_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _schedule_auto_leave(chat_id: int) -> None:
    if AUTO_LEAVE_SECS <= 0:
        return

    async def _leave():
        await asyncio.sleep(AUTO_LEAVE_SECS)
        if not currently_playing.get(chat_id) and not queues.get(chat_id):
            gc = _group_calls.get(chat_id)
            if gc:
                try:
                    await gc.stop()
                    log.info("Auto-left VC in chat %d", chat_id)
                except Exception:
                    pass

    await _cancel_auto_leave(chat_id)
    auto_leave_tasks[chat_id] = asyncio.create_task(_leave())


# ─── PLAYBACK CORE ────────────────────────────────────────────────────────────

async def _do_play(chat_id: int, track: dict) -> bool:
    """
    Full play flow:
    1. Get direct stream URL via yt-dlp
    2. Convert to PCM via ffmpeg (blocking → run_in_executor)
    3. Hand PCM file to GroupCallFile
    """
    loop = asyncio.get_running_loop()

    # Step 1: Fresh stream URL
    log.info("Getting stream URL for: %s", track["title"])
    stream_url, thumb = await loop.run_in_executor(
        None, get_stream_url, track["webpage_url"]
    )
    if not stream_url:
        log.error("No stream URL: %s", track["title"])
        return False
    if thumb:
        track["thumbnail"] = thumb

    # Step 2: Convert to PCM
    tmp_path = os.path.join(
        AUDIO_TMP_DIR, f"chat{chat_id}_{random.randint(10000, 99999)}.raw"
    )
    log.info("Converting to PCM: %s", tmp_path)
    ok = await loop.run_in_executor(None, convert_to_pcm, stream_url, tmp_path)
    if not ok:
        return False
    track["_tmp_file"] = tmp_path

    # Step 3: Play via GroupCallFile
    gc = _get_or_make_gc(chat_id)
    try:
        if gc.is_connected:
            # Already in VC — swap the file
            gc.input_filename = tmp_path
            log.info("✅ Swapped track in VC (chat %d)", chat_id)
        else:
            # Join VC and start
            gc.input_filename = tmp_path
            await gc.start(chat_id)
            log.info("✅ Joined VC and playing (chat %d)", chat_id)
        return True
    except Exception as exc:
        err = str(exc)
        log.error("❌ GroupCall error in chat %d: %s", chat_id, exc)
        if "without a voice chat" in err or "GroupCallNotFound" in err:
            log.error("💡 Voice Chat not active in group %d! Start it manually.", chat_id)
        return False


async def _play_next_locked(chat_id: int) -> bool:
    """Pop + play next track from queue. Acquires per-chat lock."""
    async with _get_lock(chat_id):
        while True:
            if not queues[chat_id]:
                currently_playing.pop(chat_id, None)
                return False
            track = queues[chat_id].pop(0)
            currently_playing[chat_id] = track
            if await _do_play(chat_id, track):
                return True
            log.warning("Skipping failed track: %s", track["title"])
            currently_playing.pop(chat_id, None)


async def play_next(chat_id: int) -> bool:
    return await _play_next_locked(chat_id)


# ─── LYRICS ───────────────────────────────────────────────────────────────────

async def fetch_lyrics(artist: str, title: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.lyrics.ovh/v1/{artist}/{title}")
            if r.status_code == 200:
                return r.json().get("lyrics", "").strip() or None
    except Exception as exc:
        log.warning("lyrics fetch failed: %s", exc)
    return None


# ─── COMMANDS ─────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name if update.effective_user else "Senpai"
    caption = (
        f"Konnichiwa *{_esc(name)}*\\-senpai\\~ 💖\n\n"
        "I'm your kawaii music bot\\! 🎵\n\n"
        "*Commands:*\n"
        "🎶 /play `<song>` — Play a song\n"
        "🔍 /search `<song>` — Pick from top 5\n"
        "⏸️ /pause — Pause\n"
        "▶️ /resume — Resume\n"
        "⏭️ /skip — Skip\n"
        "⏹️ /stop — Stop \\& clear\n"
        "📋 /queue — Show queue\n"
        "🔀 /shuffle — Shuffle\n"
        "🗑 /remove `<pos>` — Remove track\n"
        "📝 /lyrics `<song>` — Lyrics\n"
        "🔁 /loop — Toggle loop\n"
        "🎵 /np — Now playing\n"
        "🪵 /logs — Logs \\(admin\\)\n\n"
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
    msg = await update.message.reply_text(
        f"🔍 Searching *{_esc(query)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop = asyncio.get_running_loop()
    track = await loop.run_in_executor(None, search_yt, query)

    if not track:
        await msg.edit_text(
            "Gomen\\~ couldn't find that song 😢",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    queues[chat_id].append(track)
    await _cancel_auto_leave(chat_id)
    await _safe_delete(msg)

    is_idle = currently_playing.get(chat_id) is None
    if is_idle:
        loading_msg = await context.bot.send_message(
            chat_id,
            f"⏳ Loading *{_esc(track['title'])}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        started = await play_next(chat_id)
        await _safe_delete(loading_msg)

        now = currently_playing.get(chat_id, track)
        if started:
            await send_now_playing(context.bot, chat_id, now)
        else:
            await context.bot.send_message(
                chat_id,
                "Gomen\\~ playback failed 😢\n\n"
                "Check karo:\n"
                "• Group mein Voice Chat active hai?\n"
                "• Assistant account group ka member hai?\n"
                "• Server pe ffmpeg installed hai?",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    else:
        pos = len(queues[chat_id])
        caption = (
            f"✅ *Added to Queue \\#{pos}*\n\n"
            f"🎵 *{_esc(track['title'])}*\n"
            f"👤 {_esc(track.get('uploader', 'Unknown'))}\n"
            f"⏱ {_esc(_fmt_duration(track.get('duration', 0)))}"
        )
        sent = False
        if track.get("thumbnail"):
            try:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=track["thumbnail"],
                    caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
                )
                sent = True
            except Exception:
                pass
        if not sent:
            await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN_V2)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/search <song name>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    query = " ".join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Searching: *{_esc(query)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop = asyncio.get_running_loop()
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
        text += (
            f"`{i+1}.` *{_esc(r['title'])}*\n"
            f"   👤 {_esc(r.get('uploader','?'))}  "
            f"⏱ {_esc(_fmt_duration(r['duration']))}\n\n"
        )

    await msg.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def np_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = currently_playing.get(chat_id)
    if not now:
        await update.message.reply_text("Nothing is playing right now nya\\~ 🌸", parse_mode=ParseMode.MARKDOWN_V2)
        return

    q_len = len(queues[chat_id])
    loop_s = "ON 🔁" if loop_mode[chat_id] else "OFF"
    caption = (
        f"🎵 *Now Playing*\n\n*{_esc(now['title'])}*\n"
        f"👤 {_esc(now.get('uploader', 'Unknown'))}\n"
        f"⏱ {_esc(_fmt_duration(now.get('duration', 0)))}\n"
        f"🔁 Loop: {loop_s}\n"
        f"📋 Queue: {q_len} track\\(s\\) remaining"
    )
    sent = False
    if now.get("thumbnail"):
        try:
            await update.message.reply_photo(
                photo=now["thumbnail"], caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=player_keyboard(chat_id),
            )
            sent = True
        except Exception:
            pass
    if not sent:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=player_keyboard(chat_id))


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    gc = _group_calls.get(chat_id)
    if gc and gc.is_connected:
        gc.pause_playout()
        await update.message.reply_text("Music paused ⏸️")
    else:
        await update.message.reply_text("Nothing is playing right now! 🤔")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    gc = _group_calls.get(chat_id)
    if gc and gc.is_connected:
        gc.resume_playout()
        await update.message.reply_text("Music resumed! ▶️🎶")
    else:
        await update.message.reply_text("Nothing is paused right now! 🤔")


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can skip~ 🙏")
        return

    chat_id = update.effective_chat.id
    if not currently_playing.get(chat_id):
        await update.message.reply_text("Nothing to skip nya~ 🌸")
        return

    await update.message.reply_text("⏭️ Skipping...")
    gc = _group_calls.get(chat_id)
    if gc:
        gc.stop_playout()  # Stops current, triggers playout_ended

    if queues[chat_id]:
        started = await play_next(chat_id)
        now = currently_playing.get(chat_id)
        if started and now:
            await send_now_playing(context.bot, chat_id, now)
    else:
        currently_playing.pop(chat_id, None)
        if gc:
            try:
                await gc.stop()
            except Exception:
                pass
        await context.bot.send_message(chat_id, "Queue empty~ 🌸 Add more with /play!")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can stop~ 🙏")
        return

    chat_id = update.effective_chat.id
    queues[chat_id].clear()
    currently_playing.pop(chat_id, None)
    loop_mode[chat_id] = False
    await _cancel_auto_leave(chat_id)

    gc = _group_calls.pop(chat_id, None)
    if gc:
        try:
            await gc.stop()
        except Exception:
            pass
    await update.message.reply_text("Music stopped! Bye bye~ 👋")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = currently_playing.get(chat_id)
    q = queues[chat_id]

    if not now and not q:
        await update.message.reply_text("Queue is empty~ 🌸 Use /play to add songs!")
        return

    total_secs = sum(t.get("duration", 0) for t in q)
    text = "🎵 Music Queue\n\n"
    if now:
        text += f"▶️ Now: {now['title']} [{_fmt_duration(now.get('duration', 0))}]\n\n"
    if q:
        text += "📋 Up Next:\n"
        for i, track in enumerate(q[:10], 1):
            text += f"{i}. {track['title']} [{_fmt_duration(track.get('duration', 0))}]\n"
        if len(q) > 10:
            text += f"\n...and {len(q) - 10} more tracks."
        text += f"\n\n⏳ Total: {_fmt_duration(total_secs)}"
    if loop_mode[chat_id]:
        text += "\n🔁 Loop ON"
    await update.message.reply_text(text)


async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    loop_mode[chat_id] = not loop_mode[chat_id]
    state = "ON 🔁" if loop_mode[chat_id] else "OFF ▶️"
    await update.message.reply_text(f"Loop mode is now {state}!")


async def shuffle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    q = queues[chat_id]
    if not q:
        await update.message.reply_text("Queue is empty~ 🌸")
        return
    random.shuffle(q)
    await update.message.reply_text(f"🔀 Queue shuffled! {len(q)} tracks randomised~ ✨")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove <position>")
        return
    pos = int(context.args[0])
    q = queues[chat_id]
    if not q:
        await update.message.reply_text("Queue is empty~ 🌸")
        return
    if pos < 1 or pos > len(q):
        await update.message.reply_text(f"Invalid position! Queue has {len(q)} tracks.")
        return
    removed = q.pop(pos - 1)
    await update.message.reply_text(f"🗑 Removed '{removed['title']}' from position {pos}.")


async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if context.args:
        query = " ".join(context.args)
        if " - " in query:
            artist, title = query.split(" - ", 1)
        else:
            now = currently_playing.get(chat_id)
            artist = (now or {}).get("uploader", "Unknown")
            title = query
    else:
        now = currently_playing.get(chat_id)
        if not now:
            await update.message.reply_text("Nothing playing! Use: /lyrics Artist - Title")
            return
        artist = now.get("uploader", "Unknown")
        title = now.get("title", "Unknown")

    msg = await update.message.reply_text(f"📝 Fetching lyrics for '{title}'...")
    lyrics = await fetch_lyrics(artist.strip(), title.strip())
    if not lyrics:
        await msg.edit_text(f"Couldn't find lyrics for '{title}' 😢\nTry: /lyrics Artist - Song Title")
        return
    snippet = lyrics[:3500] + ("\n\n[... truncated]" if len(lyrics) > 3500 else "")
    await msg.edit_text(f"📝 {title}\n\n{snippet}")


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can view logs~ 🙏")
        return
    lines = list(_log_buffer_handler.buffer)
    if not lines:
        await update.message.reply_text("No logs yet~ 🌸")
        return
    full_text = "\n".join(lines)
    for chunk in [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]:
        await update.message.reply_text(f"📋 Recent Logs:\n\n{chunk}")


# ─── CALLBACK HANDLER ────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat.id
    data = query.data
    await query.answer()

    if data.startswith("sel:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        _, msg_id_str, idx_str = parts
        msg_id, idx = int(msg_id_str), int(idx_str)
        cache = context.bot_data.get(SEARCH_CACHE_KEY, {})
        results = cache.get(msg_id)
        if not results or idx >= len(results):
            await query.edit_message_text("Result expired~ search again!")
            return
        track = results[idx]
        cache.pop(msg_id, None)
        queues[chat_id].append(track)
        await _cancel_auto_leave(chat_id)
        await _safe_delete(query.message)

        if currently_playing.get(chat_id) is None:
            await context.bot.send_message(chat_id, f"⏳ Loading '{track['title']}'...")
            started = await play_next(chat_id)
            now = currently_playing.get(chat_id, track)
            if started:
                await send_now_playing(context.bot, chat_id, now)
            else:
                await context.bot.send_message(chat_id, "Playback failed 😢 Check VC is active!")
        else:
            pos = len(queues[chat_id])
            await context.bot.send_message(chat_id, f"✅ Added '{track['title']}' to queue at #{pos}.")
        return

    if data == "cancel_search":
        await _safe_delete(query.message)
        return

    gc = _group_calls.get(chat_id)

    if data == "pause":
        if gc:
            gc.pause_playout()

    elif data == "resume":
        if gc:
            gc.resume_playout()

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
        await query.answer(f"🔀 Shuffled {len(q)} tracks!" if q else "Queue is empty~", show_alert=False)

    elif data == "skip":
        if not currently_playing.get(chat_id):
            return
        if gc:
            gc.stop_playout()
        if queues[chat_id]:
            started = await play_next(chat_id)
            now = currently_playing.get(chat_id)
            if started and now:
                await send_now_playing(context.bot, chat_id, now)
        else:
            currently_playing.pop(chat_id, None)
            if gc:
                try:
                    await gc.stop()
                except Exception:
                    pass
            await context.bot.send_message(chat_id, "Queue empty~ 🌸 /play to add more!")

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        loop_mode[chat_id] = False
        await _cancel_auto_leave(chat_id)
        gc = _group_calls.pop(chat_id, None)
        if gc:
            try:
                await gc.stop()
            except Exception:
                pass
        await query.edit_message_text("Music stopped! Hope you enjoyed it 💖")

    elif data == "queue":
        now = currently_playing.get(chat_id)
        q = queues[chat_id]
        if not now and not q:
            await query.answer("Queue is empty!", show_alert=False)
            return
        lines: list[str] = []
        if now:
            lines.append(f"▶ {now['title']}")
        for i, t in enumerate(q[:8], 1):
            lines.append(f"{i}. {t['title']}")
        if len(q) > 8:
            lines.append(f"...+{len(q) - 8} more")
        await context.bot.send_message(chat_id, "\n".join(lines))


# ─── ERROR HANDLER ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update error:", exc_info=context.error)


# ─── STARTUP / SHUTDOWN ──────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    global _bot_app
    _bot_app = application

    await application.bot.delete_webhook(drop_pending_updates=True)
    log.info("Cleared webhook + pending updates")

    log.info("Starting Pyrogram assistant...")
    await assistant.start()
    me = await assistant.get_me()
    log.info("Assistant: %s (ID: %d)", me.first_name, me.id)

    log.info("🎀 Kawaii Music Bot v3.9 is live~!")
    log.info("Backend: pytgcalls==2.1.0 (GroupCallFactory) + ffmpeg PCM")


async def post_shutdown(application: Application) -> None:
    log.info("Shutting down...")
    for task in auto_leave_tasks.values():
        task.cancel()
    for gc in list(_group_calls.values()):
        try:
            await gc.stop()
        except Exception:
            pass
    try:
        await assistant.stop()
    except Exception:
        pass
    import shutil
    shutil.rmtree(AUDIO_TMP_DIR, ignore_errors=True)
    log.info("Bye bye~ 👋")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set!")
    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING is not set!")
    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID and API_HASH must be set!")

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
    app.add_handler(CommandHandler("shuffle", shuffle_cmd))
    app.add_handler(CommandHandler("remove",  remove_cmd))
    app.add_handler(CommandHandler("lyrics",  lyrics_cmd))
    app.add_handler(CommandHandler("logs",    logs_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=True, stop_signals=None)


if __name__ == "__main__":
    main()
