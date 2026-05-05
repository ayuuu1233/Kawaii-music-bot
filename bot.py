"""
🎀 Kawaii Telegram Music Bot v4.0
Exact compatibility:
  - py-tgcalls==2.2.11
  - pyrofork==2.3.69 (pyrogram fork)
  - python-telegram-bot==21.6

Key APIs used:
  - PyTgCalls(assistant)             ← main call client
  - call.play(chat_id, MediaStream(url), GroupCallConfig(auto_start=True))
  - call.pause(chat_id) / call.resume(chat_id)
  - call.leave_call(chat_id)
  - @call.on_update(filters.stream_end())  ← stream end handler
  - StreamEnded.chat_id              ← get chat_id from update
  - GroupCallConfig(auto_start=True) ← auto-create VC if not active
  - MediaStream handles ffmpeg internally — direct URL works!
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
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import MediaStream, StreamEnded, GroupCallConfig
from pytgcalls.types.stream import AudioQuality
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


# ─── PYROGRAM + PY-TGCALLS ───────────────────────────────────────────────────

assistant = PyrogramClient(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
call = PyTgCalls(assistant)

# Assistant ka InputPeer — post_init mein set hoga
# join_as ke liye explicitly assistant account use karo
_assistant_peer = None


# ─── STATE ────────────────────────────────────────────────────────────────────

queues:            dict[int, list[dict]]     = defaultdict(list)
currently_playing: dict[int, Optional[dict]] = {}
loop_mode:         dict[int, bool]           = defaultdict(bool)
_chat_locks:       dict[int, asyncio.Lock]   = {}
auto_leave_tasks:  dict[int, asyncio.Task]   = {}

# Set in post_init so stream-end handler can send Now Playing
_bot_app: Optional[Application] = None
_assistant_peer = None  # Assistant ka InputPeer for join_as

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
    """Highest resolution thumbnail from yt-dlp info."""
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
            try:
                await call.leave_call(chat_id)
                log.info("Auto-left VC in chat %d", chat_id)
            except Exception:
                pass

    await _cancel_auto_leave(chat_id)
    auto_leave_tasks[chat_id] = asyncio.create_task(_leave())


# ─── PLAYBACK ─────────────────────────────────────────────────────────────────

async def _ensure_assistant_in_group(chat_id: int) -> bool:
    """
    Agar assistant group mein nahi hai toh auto-join karo.
    Supergroup IDs (-100xxxxxxxxxx) ko properly handle karta hai.
    Returns True agar assistant group mein hai (ya join ho gaya).
    """
    me = await assistant.get_me()

    # Step 1: Already member hai? Check karo
    try:
        member = await assistant.get_chat_member(chat_id, me.id)
        # Kicked/banned toh False return karo
        if hasattr(member, "status"):
            status = str(member.status)
            if "kicked" in status.lower() or "banned" in status.lower():
                log.error("❌ Assistant is banned/kicked from chat %d", chat_id)
                return False
        log.info("✅ Assistant already in group %d", chat_id)
        return True
    except Exception as e:
        err = str(e)
        # USER_NOT_PARTICIPANT = nahi hai, baaki errors = aur kuch
        if "USER_NOT_PARTICIPANT" not in err and "user_not_participant" not in err.lower():
            log.info("get_chat_member check: %s — trying to join anyway", err)

    # Step 2: Chat info fetch karo — peer resolve karke
    username = None
    invite_link = None
    try:
        # Supergroup negative ID ko int mein convert karke try karo
        try:
            peer = await assistant.resolve_peer(chat_id)
            chat = await assistant.get_chat(chat_id)
        except Exception:
            # Fallback: string ID try karo
            chat = await assistant.get_chat(str(chat_id))

        username = getattr(chat, "username", None)
        invite_link = getattr(chat, "invite_link", None)
    except Exception as e:
        log.warning("Could not fetch chat info for %d: %s", chat_id, e)

    # Step 3: Join karo
    join_target = username or invite_link
    if join_target:
        try:
            await assistant.join_chat(join_target)
            log.info("✅ Assistant joined group %d via: %s", chat_id, join_target)
            return True
        except Exception as exc:
            joined_err = str(exc)
            if "already" in joined_err.lower() or "USER_ALREADY_PARTICIPANT" in joined_err:
                log.info("✅ Assistant already in group %d (join confirmed)", chat_id)
                return True
            log.error("❌ Auto-join failed for chat %d: %s", chat_id, exc)
            return False

    # Step 4: Username/link nahi mila — bot se invite link generate karne ki request
    log.error("❌ Cannot auto-join chat %d — no username/invite link. Add assistant manually.", chat_id)
    return False


async def _do_play(chat_id: int, track: dict) -> bool:
    """
    1. Ensure assistant is in the group (auto-join if needed)
    2. Get fresh stream URL via yt-dlp
    3. Play via MediaStream (ffmpeg handled internally)
    GroupCallConfig(auto_start=True) auto-creates VC if not active.
    """
    loop = asyncio.get_running_loop()

    # Step 1: Assistant group mein hona chahiye
    in_group = await _ensure_assistant_in_group(chat_id)
    if not in_group:
        return False

    # Step 2: Fresh stream URL + updated thumbnail
    log.info("Getting stream URL: %s", track["title"])
    stream_url, thumb = await loop.run_in_executor(
        None, get_stream_url, track["webpage_url"]
    )
    if not stream_url:
        log.error("No stream URL for: %s", track["title"])
        return False
    if thumb:
        track["thumbnail"] = thumb

    try:
        stream = MediaStream(
            stream_url,
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,  # audio-only
        )
    except Exception as exc:
        log.error("MediaStream creation failed: %s", exc)
        return False

    config = GroupCallConfig(
        auto_start=True,
        join_as=_assistant_peer,  # Assistant account join kare VC mein, bot nahi
    )

    try:
        await call.play(chat_id, stream, config)
        log.info("✅ Playing in chat %d: %s", chat_id, track["title"])
        return True
    except Exception as exc:
        err = str(exc)
        log.error("❌ call.play() error in chat %d: %s", chat_id, exc)
        if "NoActiveGroupCall" in err:
            log.error("💡 VC could not be auto-created in chat %d. Check bot admin perms.", chat_id)
        elif "PARTICIPANT_JOIN_MISSING" in err or "not in the group" in err.lower():
            log.error("💡 Assistant join failed for chat %d.", chat_id)
        return False


async def play_next(chat_id: int) -> bool:
    """Pop + play next track. Acquires per-chat lock to prevent races."""
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
            # Loop: try next track


# ─── STREAM END HANDLER ──────────────────────────────────────────────────────

@call.on_update(filters.stream_end())
async def on_stream_end(client: PyTgCalls, update: StreamEnded) -> None:
    """
    Fires when current track ends naturally.
    update.chat_id gives us the group.
    update.stream_type is AUDIO or VIDEO.
    """
    # Only care about audio end
    if update.stream_type != StreamEnded.Type.AUDIO:
        return

    chat_id: int = update.chat_id
    log.info("🎵 Stream ended in chat %d", chat_id)

    # Loop mode: re-add current track to queue before popping
    current = currently_playing.get(chat_id)
    if current and loop_mode[chat_id]:
        queues[chat_id].append(current)

    currently_playing.pop(chat_id, None)

    if queues.get(chat_id):
        started = await play_next(chat_id)
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
                "• Assistant account group mein add hai?\n"
                "• Bot ko manage voice chat permission hai?",
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
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=player_keyboard(chat_id),
            )
            sent = True
        except Exception:
            pass
    if not sent:
        await update.message.reply_text(
            caption, parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=player_keyboard(chat_id),
        )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        await call.pause(chat_id)
        await update.message.reply_text("Music paused ⏸️")
    except Exception:
        await update.message.reply_text("Nothing is playing right now! 🤔")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        await call.resume(chat_id)
        await update.message.reply_text("Music resumed! ▶️🎶")
    except Exception:
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

    if queues[chat_id]:
        started = await play_next(chat_id)
        now = currently_playing.get(chat_id)
        if started and now:
            await send_now_playing(context.bot, chat_id, now)
    else:
        currently_playing.pop(chat_id, None)
        try:
            await call.leave_call(chat_id)
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
    try:
        await call.leave_call(chat_id)
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

    if data == "pause":
        try:
            await call.pause(chat_id)
        except Exception:
            pass

    elif data == "resume":
        try:
            await call.resume(chat_id)
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
        await query.answer(f"🔀 Shuffled {len(q)} tracks!" if q else "Queue is empty~", show_alert=False)

    elif data == "skip":
        if not currently_playing.get(chat_id):
            return
        if queues[chat_id]:
            started = await play_next(chat_id)
            now = currently_playing.get(chat_id)
            if started and now:
                await send_now_playing(context.bot, chat_id, now)
        else:
            currently_playing.pop(chat_id, None)
            try:
                await call.leave_call(chat_id)
            except Exception:
                pass
            await context.bot.send_message(chat_id, "Queue empty~ 🌸 /play to add more!")

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        loop_mode[chat_id] = False
        await _cancel_auto_leave(chat_id)
        try:
            await call.leave_call(chat_id)
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
    global _bot_app, _assistant_peer
    _bot_app = application

    await application.bot.delete_webhook(drop_pending_updates=True)
    log.info("Cleared webhook + pending updates")

    log.info("Starting Pyrogram (pyrofork) assistant...")
    await assistant.start()
    me = await assistant.get_me()
    log.info("Assistant: %s (ID: %d)", me.first_name, me.id)

    # Assistant ka InputPeer fetch karo taaki VC mein assistant join kare, bot nahi
    try:
        _assistant_peer = await assistant.resolve_peer(me.id)
        log.info("Assistant peer resolved for join_as: ID=%d", me.id)
    except Exception as e:
        log.warning("Could not resolve assistant peer: %s", e)
        _assistant_peer = None

    log.info("Starting PyTgCalls...")
    await call.start()
    log.info("🎀 Kawaii Music Bot v4.0 is live~!")
    log.info("join_as = assistant account (not bot)"  )


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
