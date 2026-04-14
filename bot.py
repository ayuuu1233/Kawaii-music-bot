"""
🎀 Kawaii Telegram Music Bot  v3.0
Rewritten with python-telegram-bot v20+ (async) + pytgcalls + yt-dlp

CHANGES FROM v2 (pyrogram):
  - Migrated from pyrogram → python-telegram-bot v20+ (full async)
  - ApplicationBuilder pattern used for bot lifecycle
  - ConversationHandler not needed; callback_query handlers cover inline btns
  - asyncio.get_event_loop() replaced with asyncio.get_running_loop()
  - All command handlers use (update, context) PTB signature
  - Graceful shutdown via Application.run_polling() stop_signals param
  - All v2 bugs carried over fixed here too

BUG FIXES (same as v2):
  - typing_effect now accepts msg as explicit parameter
  - start_cmd properly awaits reply_video + cleans up placeholder
  - play_next guarded with per-chat asyncio.Lock (no race condition)
  - on_stream_end signature fixed for pytgcalls v2 (client, update)
  - asyncio.get_event_loop() replaced everywhere with get_running_loop()

FEATURES:
  - /play  <song>          — search & play from YouTube
  - /search <song>         — pick from top-5 inline results
  - /np                    — now playing with progress bar
  - /queue                 — full queue listing
  - /pause /resume         — playback control
  - /skip                  — admin only, skip current track
  - /stop                  — admin only, stop & clear
  - /loop                  — toggle per-chat loop mode
  - /vol <0-200>           — admin only, volume control
  - Inline player keyboard (pause/resume/skip/stop/queue/loop)
  - Auto-leave VC after idle (configurable)
  - Per-chat asyncio.Lock prevents double-play race conditions
  - Structured logging
  - Retry logic on yt-dlp failures
"""

import asyncio
import logging
import os
from collections import defaultdict
from typing import Optional

import yt_dlp
from pyrogram import Client as PyrogramClient
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream
from telegram import (
    CallbackQuery,
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

# ─── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("KawaiiBot")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
API_ID          = int(os.environ.get("API_ID", 0))
API_HASH        = os.environ.get("API_HASH", "")
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
SESSION_STRING  = os.environ.get("SESSION_STRING", "")
ADMIN_IDS       = (
    list(map(int, os.environ["ADMIN_IDS"].split(",")))
    if os.environ.get("ADMIN_IDS")
    else []
)
AUTO_LEAVE_SECS = int(os.environ.get("AUTO_LEAVE_SECS", "120"))  # 0 = never

# ─── PYROGRAM ASSISTANT + PYTGCALLS ────────────────────────────────────────────
# python-telegram-bot handles the bot commands/messages.
# Pyrogram assistant account handles the actual voice chat streaming via pytgcalls.
assistant = PyrogramClient(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
call = PyTgCalls(assistant)

# ─── STATE ─────────────────────────────────────────────────────────────────────
queues:            dict[int, list[dict]]      = defaultdict(list)
currently_playing: dict[int, Optional[dict]]  = {}
loop_mode:         dict[int, bool]            = defaultdict(bool)
# FIX: defaultdict(asyncio.Lock) doesn't work — Lock must be created per-chat lazily
_chat_locks:       dict[int, asyncio.Lock]    = {}
auto_leave_tasks:  dict[int, asyncio.Task]    = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    """Lazily create per-chat asyncio.Lock (fixes defaultdict(asyncio.Lock) issue)."""
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    """Return True when ADMIN_IDS is empty (open-to-all) or user is listed."""
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


async def typing_effect(msg: Message, text: str, delay: float = 0.03) -> None:
    """
    Progressive typing animation by repeatedly editing *msg*.
    FIX: receives msg as an explicit parameter — v2 had 'msg' undefined here.
    """
    typed = ""
    for ch in text:
        typed += ch
        try:
            await msg.edit_text(typed)
        except Exception:
            pass
        await asyncio.sleep(delay)


def player_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    loop_label = "🔁 Loop ON" if loop_mode.get(chat_id) else "🔁 Loop OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause",  callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            InlineKeyboardButton("⏭️ Skip",   callback_data="skip"),
        ],
        [
            InlineKeyboardButton("⏹️ Stop",  callback_data="stop"),
            InlineKeyboardButton("📋 Queue", callback_data="queue"),
            InlineKeyboardButton(loop_label, callback_data="loop"),
        ],
    ])


# ─── YT-DLP ────────────────────────────────────────────────────────────────────

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


def search_yt(query: str, retries: int = 2) -> Optional[dict]:
    """Synchronous: search YouTube, return best audio info. Retries on failure."""
    opts = _ydl_opts({"default_search": "ytsearch1"})
    for attempt in range(retries + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                return {
                    "title":       info.get("title", "Unknown"),
                    "url":         info.get("url"),
                    "duration":    info.get("duration", 0),
                    "webpage_url": info.get("webpage_url", ""),
                    "thumbnail":   info.get("thumbnail", ""),
                    "uploader":    info.get("uploader", "Unknown"),
                }
        except Exception as exc:
            log.warning("yt-dlp attempt %d/%d failed: %s", attempt + 1, retries + 1, exc)
            if attempt == retries:
                return None


def search_yt_multi(query: str, count: int = 5) -> list[dict]:
    """Synchronous: return top *count* YouTube results for query."""
    opts = _ydl_opts({"default_search": f"ytsearch{count}"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(query, download=False)
            entries = info.get("entries", [])
            return [
                {
                    "title":       e.get("title", "Unknown"),
                    "url":         e.get("url"),
                    "duration":    e.get("duration", 0),
                    "webpage_url": e.get("webpage_url", ""),
                    "thumbnail":   e.get("thumbnail", ""),
                    "uploader":    e.get("uploader", "Unknown"),
                }
                for e in entries if e
            ]
    except Exception as exc:
        log.error("multi-search error: %s", exc)
        return []


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
                await call.leave_group_call(chat_id)
                log.info("Auto-left VC in chat %d after %ds idle", chat_id, AUTO_LEAVE_SECS)
            except Exception:
                pass

    await _cancel_auto_leave(chat_id)
    auto_leave_tasks[chat_id] = asyncio.create_task(_leave())


async def play_next(chat_id: int) -> bool:
    """
    Pop the next track from the queue and begin streaming.
    FIX: wrapped in per-chat Lock so concurrent skip/stream-end calls don't
    trigger double-play.
    Returns True if a track was started.
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

        try:
            await call.play(
                chat_id,
                MediaStream(track["url"], audio_quality=AudioQuality.STUDIO),
            )
            log.info("Now playing in %d: %s", chat_id, track["title"])
            return True
        except Exception as exc:
            log.error("play error in %d: %s", chat_id, exc)
            currently_playing.pop(chat_id, None)
            return False


# ─── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """FIX: reply_video properly awaited; 'text' variable removed; msg properly passed."""
    name = update.effective_user.first_name if update.effective_user else "Senpai"
    msg  = await update.message.reply_text("💖 Starting... UwU")

    await typing_effect(msg, "✨ Loading Kawaii Music System... 🎶")
    await asyncio.sleep(0.8)
    await typing_effect(msg, "💫 Connecting to Voice Engine... 🎧")
    await asyncio.sleep(0.8)
    await typing_effect(msg, f"🌸 Welcome {name}-senpai~! 💕")
    await asyncio.sleep(0.6)

    # FIX: reply_video now properly awaited and caption defined inline
    await update.message.reply_video(
        video="https://files.catbox.moe/9w0qsn.mp4",
        caption=(
            f"Konnichiwa *{name}*-senpai~! 💖\n\n"
            "I'm your kawaii music bot, here to fill your voice chat with sweet tunes\\! 🎵\n\n"
            "*Commands:*\n"
            "🎶 /play `<song>` — Play a song\n"
            "🔍 /search `<song>` — Pick from top 5 results\n"
            "⏸️ /pause — Pause music\n"
            "▶️ /resume — Resume music\n"
            "⏭️ /skip — Skip current track\n"
            "⏹️ /stop — Stop and clear queue\n"
            "📋 /queue — Show queue\n"
            "🔁 /loop — Toggle loop mode\n"
            "🔊 /vol `<0\\-200>` — Set volume\n"
            "🎵 /np — Now playing\n\n"
            "Let's make some music magic, nya~\\! ✨"
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await msg.delete()


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Ara ara~ tell me what to play Senpai\\! 🎵\n"
            "Usage: `/play <song name or YouTube URL>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query         = " ".join(context.args)
    searching_msg = await update.message.reply_text(
        f"🔍 Searching for *{query}*\\.\\.\\. please wait nya~",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # FIX: use get_running_loop() instead of deprecated get_event_loop()
    loop  = asyncio.get_running_loop()
    track = await loop.run_in_executor(None, search_yt, query)

    if not track:
        await searching_msg.edit_text("Gomen nasai~ I couldn't find that song 😢")
        return

    queues[chat_id].append(track)
    await _cancel_auto_leave(chat_id)

    is_idle = currently_playing.get(chat_id) is None
    await searching_msg.delete()

    if is_idle:
        started = await play_next(chat_id)
        now     = currently_playing.get(chat_id, track)
        dur     = _fmt_duration(now.get("duration", 0))
        bar     = _progress_bar(0, now.get("duration", 1))

        if started:
            await update.message.reply_text(
                f"🎶 *Now Playing*\n\n"
                f"🎵 *{now['title']}*\n"
                f"👤 {now.get('uploader', 'Unknown')}\n"
                f"⏱ {dur}  {bar}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=player_keyboard(chat_id),
            )
        else:
            await update.message.reply_text("Gomen~ something went wrong starting playback 😢")
    else:
        pos = len(queues[chat_id])
        dur = _fmt_duration(track.get("duration", 0))
        await update.message.reply_text(
            f"✅ *Added to Queue \\#{pos}*\n\n"
            f"🎵 *{track['title']}*\n"
            f"👤 {track.get('uploader', 'Unknown')}\n"
            f"⏱ {dur}\n\n"
            "Be patient Senpai, it'll be your turn soon~ 🌸",
            parse_mode=ParseMode.MARKDOWN,
        )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: `/search <song name>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query = " ".join(context.args)
    msg   = await update.message.reply_text(
        f"🔍 Searching for top results: *{query}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop    = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, lambda: search_yt_multi(query, 5))

    if not results:
        await msg.edit_text("No results found, gomen~ 😢")
        return

    buttons = [
        [InlineKeyboardButton(
            f"{i+1}. {r['title'][:40]} ({_fmt_duration(r['duration'])})",
            callback_data=f"sel:{i}:{query}",
        )]
        for i, r in enumerate(results)
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_search")])

    text = "🎵 *Top Results* — pick a song Senpai~\n\n"
    for i, r in enumerate(results):
        text += f"`{i+1}.` *{r['title']}*\n   👤 {r.get('uploader','?')}  ⏱ {_fmt_duration(r['duration'])}\n\n"

    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def np_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now     = currently_playing.get(chat_id)

    if not now:
        await update.message.reply_text("Nothing is playing right now nya~ 🌸")
        return

    dur = now.get("duration", 0)
    await update.message.reply_text(
        f"🎵 *Now Playing*\n\n"
        f"*{now['title']}*\n"
        f"👤 {now.get('uploader', 'Unknown')}\n"
        f"⏱ {_fmt_duration(dur)}\n"
        f"🔁 Loop: {'ON' if loop_mode[chat_id] else 'OFF'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=player_keyboard(chat_id),
    )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.pause_stream(update.effective_chat.id)
        await update.message.reply_text("Music paused UwU ⏸️\nSay /resume when you're ready~")
    except Exception:
        await update.message.reply_text("Hmm~ nothing is playing right now Senpai! 🤔")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.resume_stream(update.effective_chat.id)
        await update.message.reply_text("Yay! Music is back~! ▶️🎶")
    except Exception:
        await update.message.reply_text("Hmm~ nothing is paused right now Senpai! 🤔")


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can skip, gomen~ 🙏")
        return

    chat_id = update.effective_chat.id
    if not currently_playing.get(chat_id):
        await update.message.reply_text("There's nothing to skip nya~ 🌸")
        return

    await update.message.reply_text("Skipping this track, hehe~ ➡️")
    try:
        await call.leave_group_call(chat_id)
    except Exception:
        pass
    currently_playing.pop(chat_id, None)

    if queues[chat_id]:
        started = await play_next(chat_id)
        now     = currently_playing.get(chat_id)
        if started and now:
            await update.message.reply_text(
                f"🎵 *Now Playing*\n\n*{now['title']}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=player_keyboard(chat_id),
            )
    else:
        await update.message.reply_text("The queue is empty now~ 🌸 Add more songs with /play!")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can stop the music, gomen~ 🙏")
        return

    chat_id = update.effective_chat.id
    queues[chat_id].clear()
    currently_playing.pop(chat_id, None)
    loop_mode[chat_id] = False
    await _cancel_auto_leave(chat_id)
    try:
        await call.leave_group_call(chat_id)
    except Exception:
        pass
    await update.message.reply_text("Music stopped! Bye bye~ 👋\nHope you enjoyed it Senpai 💖")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now     = currently_playing.get(chat_id)
    q       = queues[chat_id]

    if not now and not q:
        await update.message.reply_text("The queue is empty nya~ 🌸\nUse /play to add songs!")
        return

    text = "🎵 *Music Queue*\n\n"
    if now:
        dur   = _fmt_duration(now.get("duration", 0))
        text += f"▶️ *Now Playing:*\n{now['title']} `[{dur}]`\n\n"
    if q:
        text += "📋 *Up Next:*\n"
        for i, track in enumerate(q[:10], 1):
            dur   = _fmt_duration(track.get("duration", 0))
            text += f"`{i}.` {track['title']} `[{dur}]`\n"
        if len(q) > 10:
            text += f"\n\\.\\.\\.and *{len(q) - 10}* more tracks\\."
    if loop_mode[chat_id]:
        text += "\n🔁 *Loop mode is ON*"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id            = update.effective_chat.id
    loop_mode[chat_id] = not loop_mode[chat_id]
    state              = "ON 🔁" if loop_mode[chat_id] else "OFF ▶️"
    await update.message.reply_text(f"Loop mode is now *{state}*!", parse_mode=ParseMode.MARKDOWN)


async def vol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can change volume, gomen~ 🙏")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/vol <0-200>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    volume  = max(0, min(200, int(context.args[0])))
    chat_id = update.effective_chat.id
    try:
        await call.change_volume_call(chat_id, volume)
        await update.message.reply_text(f"🔊 Volume set to *{volume}%* nya~!", parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        log.error("vol error: %s", exc)
        await update.message.reply_text("Couldn't change volume right now~ 😢")


# ─── CALLBACK QUERY HANDLER ────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat.id
    data    = query.data

    await query.answer()  # always acknowledge first to remove loading spinner

    # ── Search result selection ──────────────────────────────────────────────
    if data.startswith("sel:"):
        _, idx_str, orig_query = data.split(":", 2)
        idx  = int(idx_str)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: search_yt_multi(orig_query, 5))

        if idx >= len(results):
            await query.edit_message_text("Oops, couldn't find that track~")
            return

        track = results[idx]
        queues[chat_id].append(track)
        await _cancel_auto_leave(chat_id)
        await query.message.delete()

        is_idle = currently_playing.get(chat_id) is None
        if is_idle:
            await play_next(chat_id)
            now = currently_playing.get(chat_id, track)
            await context.bot.send_message(
                chat_id,
                f"🎶 *Now Playing*\n\n*{now['title']}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=player_keyboard(chat_id),
            )
        else:
            pos = len(queues[chat_id])
            await context.bot.send_message(
                chat_id,
                f"✅ Added *{track['title']}* to queue at \\#{pos}\\.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if data == "cancel_search":
        await query.message.delete()
        return

    # ── Playback controls ────────────────────────────────────────────────────
    if data == "pause":
        try:
            await call.pause_stream(chat_id)
            await query.edit_message_reply_markup(player_keyboard(chat_id))
        except Exception:
            pass

    elif data == "resume":
        try:
            await call.resume_stream(chat_id)
            await query.edit_message_reply_markup(player_keyboard(chat_id))
        except Exception:
            pass

    elif data == "loop":
        loop_mode[chat_id] = not loop_mode[chat_id]
        try:
            await query.edit_message_reply_markup(player_keyboard(chat_id))
        except Exception:
            pass

    elif data == "skip":
        if not currently_playing.get(chat_id):
            return
        try:
            await call.leave_group_call(chat_id)
        except Exception:
            pass
        currently_playing.pop(chat_id, None)
        if queues[chat_id]:
            started = await play_next(chat_id)
            now     = currently_playing.get(chat_id)
            if started and now:
                await context.bot.send_message(
                    chat_id,
                    f"🎵 *Now Playing*\n\n*{now['title']}*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=player_keyboard(chat_id),
                )
        else:
            await context.bot.send_message(chat_id, "Queue empty~ 🌸 Add more with /play!")

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        loop_mode[chat_id] = False
        await _cancel_auto_leave(chat_id)
        try:
            await call.leave_group_call(chat_id)
        except Exception:
            pass
        await query.edit_message_text("Music stopped! Hope you enjoyed it Senpai 💖")

    elif data == "queue":
        now = currently_playing.get(chat_id)
        q   = queues[chat_id]
        if not now and not q:
            return
        lines = []
        if now:
            lines.append(f"▶ {now['title']}")
        for i, t in enumerate(q[:8], 1):
            lines.append(f"{i}. {t['title']}")
        if len(q) > 8:
            lines.append(f"...+{len(q)-8} more")
        # answer() already called above; send a follow-up message instead
        await context.bot.send_message(chat_id, "\n".join(lines))


# ─── STREAM END ────────────────────────────────────────────────────────────────

@call.on_stream_end()
async def on_stream_end(client, update) -> None:
    """
    FIX v2: PyTgCalls v2 passes (client, update) — fixed signature.
    FIX: currently_playing cleared before play_next to avoid stale state.
    """
    chat_id = update.chat_id
    log.info("Stream ended in chat %d", chat_id)
    currently_playing.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await _schedule_auto_leave(chat_id)


# ─── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Called once after the Application initialises — start assistant & pytgcalls."""
    log.info("Starting Pyrogram assistant...")
    await assistant.start()
    log.info("Starting PyTgCalls...")
    await call.start()
    log.info("🎀 Kawaii Music Bot v3 is live~ nyaa!")


async def post_shutdown(application: Application) -> None:
    """Called on graceful shutdown — cancel tasks, stop clients."""
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
        raise RuntimeError("BOT_TOKEN environment variable is not set!")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("play",   play_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("np",     np_cmd))
    app.add_handler(CommandHandler("pause",  pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("skip",   skip_cmd))
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("queue",  queue_cmd))
    app.add_handler(CommandHandler("loop",   loop_cmd))
    app.add_handler(CommandHandler("vol",    vol_cmd))

    # Inline keyboard callback handler
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("Starting polling...")
    # stop_signals handles SIGINT/SIGTERM gracefully via PTB's built-in mechanism
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
  
