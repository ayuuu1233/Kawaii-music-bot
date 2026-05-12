"""
🎵 ZenixMusic Bot v5.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Platforms  : YouTube · SoundCloud · Spotify · JioSaavn · Apple Music · Deezer
Framework  : py-tgcalls==2.2.11 + pyrofork + python-telegram-bot==21.6
Features   :
  • Multi-platform search & stream
  • Professional Telegram music bot UI (English)
  • Auto-join group if assistant not member
  • Auto-create Voice Chat (GroupCallConfig auto_start=True)
  • join_as = assistant account (not bot)
  • Queue, Loop, Shuffle, Remove, Now Playing
  • HD Thumbnails from all platforms
  • Lyrics via lyrics.ovh
  • Admin controls
  • Health server for Render/Railway
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import collections
import logging
import os
import random
import re
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import httpx
import yt_dlp
from bgm_extractor import register_bgm_handlers
from pyrogram import Client as PyrogramClient
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import GroupCallConfig, MediaStream, StreamEnded
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

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

class LogBufferHandler(logging.Handler):
    def __init__(self, maxlen: int = 100):
        super().__init__()
        self.buffer: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


_log_handler = LogBufferHandler(maxlen=100)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ZenixMusic")
logging.getLogger().addHandler(_log_handler)

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

API_ID         = int(os.environ.get("API_ID", 0))
API_HASH       = os.environ.get("API_HASH", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
ADMIN_IDS: list[int] = (
    list(map(int, os.environ["ADMIN_IDS"].split(",")))
    if os.environ.get("ADMIN_IDS") else []
)
AUTO_LEAVE_SECS = int(os.environ.get("AUTO_LEAVE_SECS", "180"))
COOKIES_FILE    = os.environ.get("COOKIES_FILE", "cookies.txt")
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# Search sources in priority order
SEARCH_SOURCES = ["ytsearch1", "scsearch1"]

# ══════════════════════════════════════════════════════════════
#  CLIENTS
# ══════════════════════════════════════════════════════════════

assistant = PyrogramClient(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
call = PyTgCalls(assistant)

# ══════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════

queues:            dict[int, list[dict]]     = defaultdict(list)
currently_playing: dict[int, Optional[dict]] = {}
loop_mode:         dict[int, bool]           = defaultdict(bool)
_chat_locks:       dict[int, asyncio.Lock]   = {}
auto_leave_tasks:  dict[int, asyncio.Task]   = {}
_bot_app:          Optional[Application]     = None
_assistant_peer                              = None
_spotify_token:    Optional[str]             = None

SEARCH_CACHE_KEY = "search_cache"


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]

# ══════════════════════════════════════════════════════════════
#  HEALTH SERVER
# ══════════════════════════════════════════════════════════════

def _run_health_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ZenixMusic is alive!")
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a): pass

    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), Handler).serve_forever()

# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def _fmt(secs: int) -> str:
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _esc(t: str) -> str:
    for c in r"\_*[]()~`>#+-=|{}.!":
        t = t.replace(c, f"\\{c}")
    return t


def _platform_icon(source: str) -> str:
    s = (source or "").lower()
    if "spotify"   in s: return "🟢"
    if "soundcloud" in s: return "🟠"
    if "jiosaavn"  in s: return "🎵"
    if "apple"     in s: return "🍎"
    if "deezer"    in s: return "💜"
    return "🔴"  # YouTube default


async def _safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════

def player_kb(chat_id: int) -> InlineKeyboardMarkup:
    loop_btn = "🔁  Loop: ON" if loop_mode.get(chat_id) else "🔁  Loop: OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸  Pause",   callback_data="pause"),
            InlineKeyboardButton("▶️  Resume",  callback_data="resume"),
            InlineKeyboardButton("⏭  Skip",    callback_data="skip"),
        ],
        [
            InlineKeyboardButton("⏹  Stop",    callback_data="stop"),
            InlineKeyboardButton("📋  Queue",   callback_data="queue"),
            InlineKeyboardButton(loop_btn,      callback_data="loop"),
        ],
        [
            InlineKeyboardButton("🔀  Shuffle", callback_data="shuffle"),
            InlineKeyboardButton("🎵  Now Playing", callback_data="np"),
        ],
    ])

# ══════════════════════════════════════════════════════════════
#  SPOTIFY TOKEN
# ══════════════════════════════════════════════════════════════

async def _refresh_spotify_token() -> Optional[str]:
    global _spotify_token
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        import base64
        creds = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            )
            if r.status_code == 200:
                _spotify_token = r.json().get("access_token")
                log.info("Spotify token refreshed")
                return _spotify_token
    except Exception as e:
        log.warning("Spotify token refresh failed: %s", e)
    return None


async def _spotify_search(query: str) -> Optional[dict]:
    """Search Spotify, return track info for yt-dlp to stream."""
    token = _spotify_token or await _refresh_spotify_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query, "type": "track", "limit": 1},
            )
            if r.status_code == 401:
                await _refresh_spotify_token()
                return None
            data = r.json()
            items = data.get("tracks", {}).get("items", [])
            if not items:
                return None
            t = items[0]
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            title   = t.get("name", "Unknown")
            album   = t.get("album", {})
            # Thumbnail: largest image
            images  = sorted(
                album.get("images", []),
                key=lambda x: x.get("width", 0), reverse=True
            )
            thumb = images[0]["url"] if images else ""
            duration_ms = t.get("duration_ms", 0)
            return {
                "title":       f"{artists} - {title}",
                "search_query": f"{artists} {title}",
                "duration":    duration_ms // 1000,
                "thumbnail":   thumb,
                "uploader":    artists,
                "source":      "Spotify",
                # webpage_url will be filled by yt-dlp search
            }
    except Exception as e:
        log.warning("Spotify search error: %s", e)
    return None


async def _jiosaavn_search(query: str) -> Optional[dict]:
    """Search JioSaavn API."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://saavn.dev/api/search/songs",
                params={"query": query, "page": 1, "limit": 1},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            results = data.get("data", {}).get("results", [])
            if not results:
                return None
            s = results[0]
            artists = ", ".join(
                a.get("name", "") for a in s.get("artists", {}).get("primary", [])
            )
            title = s.get("name", "Unknown")
            images = s.get("image", [])
            thumb = ""
            if images:
                # Get highest quality
                thumb = sorted(images, key=lambda x: x.get("quality", ""), reverse=True)[0].get("url", "")
            # Download URL (if available directly)
            dl_urls = s.get("downloadUrl", [])
            direct_url = ""
            if dl_urls:
                best = sorted(dl_urls, key=lambda x: x.get("quality", ""), reverse=True)
                direct_url = best[0].get("url", "")
            duration = int(s.get("duration", 0))
            return {
                "title":       f"{artists} - {title}" if artists else title,
                "search_query": f"{artists} {title}",
                "duration":    duration,
                "thumbnail":   thumb,
                "uploader":    artists or "JioSaavn",
                "source":      "JioSaavn",
                "direct_url":  direct_url,  # may be usable directly
            }
    except Exception as e:
        log.warning("JioSaavn search error: %s", e)
    return None

# ══════════════════════════════════════════════════════════════
#  YT-DLP
# ══════════════════════════════════════════════════════════════

def _ydl_opts(extra: dict | None = None) -> dict:
    base = {
        "format":         "bestaudio[ext=m4a]/bestaudio/best",
        "quiet":          True,
        "no_warnings":    True,
        "noplaylist":     True,
        "source_address": "0.0.0.0",
    }
    if os.path.isfile(COOKIES_FILE):
        base["cookiefile"] = COOKIES_FILE
    if extra:
        base.update(extra)
    return base


def _best_thumbnail(info: dict) -> str:
    thumbs = info.get("thumbnails") or []
    valid = [
        (t.get("width", 0) * t.get("height", 0), t["url"])
        for t in thumbs if t.get("url", "").startswith("http")
    ]
    if valid:
        return max(valid, key=lambda x: x[0])[1]
    return info.get("thumbnail", "")


def _ydl_search_one(query: str) -> Optional[dict]:
    """Search via yt-dlp across YouTube + SoundCloud."""
    for source in SEARCH_SOURCES:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts({"default_search": source})) as ydl:
                info = ydl.extract_info(query, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                url = info.get("webpage_url") or info.get("url")
                if not url:
                    continue
                src = "SoundCloud" if "soundcloud" in url else "YouTube"
                return {
                    "title":       info.get("title", "Unknown"),
                    "webpage_url": url,
                    "duration":    info.get("duration", 0),
                    "thumbnail":   _best_thumbnail(info),
                    "uploader":    info.get("uploader", "Unknown"),
                    "source":      src,
                }
        except Exception as exc:
            log.warning("[%s] search failed: %s", source, exc)
    return None


def _ydl_search_multi(query: str, count: int = 6) -> list[dict]:
    results = []
    for source in [f"ytsearch{count}", f"scsearch{count}"]:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts({"default_search": source})) as ydl:
                info = ydl.extract_info(query, download=False)
                for e in info.get("entries", []):
                    if not e:
                        continue
                    url = e.get("webpage_url") or e.get("url", "")
                    if not url:
                        continue
                    src = "SoundCloud" if "soundcloud" in url else "YouTube"
                    results.append({
                        "title":       e.get("title", "Unknown"),
                        "webpage_url": url,
                        "duration":    e.get("duration", 0),
                        "thumbnail":   _best_thumbnail(e),
                        "uploader":    e.get("uploader", "Unknown"),
                        "source":      src,
                    })
                if results:
                    break
        except Exception as exc:
            log.warning("multi-search [%s]: %s", source, exc)
    return results[:count]


def _get_stream_url(webpage_url: str) -> tuple[str, str]:
    """Returns (direct_audio_url, updated_thumbnail)."""
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

# ══════════════════════════════════════════════════════════════
#  SMART SEARCH  (multi-platform)
# ══════════════════════════════════════════════════════════════

def _detect_url_platform(url: str) -> Optional[str]:
    if "spotify.com"   in url: return "spotify"
    if "youtu"         in url: return "youtube"
    if "soundcloud.com" in url: return "soundcloud"
    if "jiosaavn.com"  in url: return "jiosaavn"
    if "apple.com/music" in url or "music.apple" in url: return "apple"
    if "deezer.com"    in url: return "deezer"
    return None


async def smart_search(query: str) -> Optional[dict]:
    """
    Multi-platform search with fallback chain:
    Spotify → JioSaavn → YouTube/SoundCloud (yt-dlp)
    Direct URLs are handled by yt-dlp directly.
    """
    loop = asyncio.get_running_loop()

    # Direct URL?
    if query.startswith("http"):
        platform = _detect_url_platform(query)
        if platform == "spotify":
            # Extract track name from Spotify URL via API
            pass  # fall through to yt-dlp with URL
        # yt-dlp handles YouTube, SoundCloud, Deezer, Apple Music URLs
        result = await loop.run_in_executor(None, _ydl_search_one, query)
        if result:
            return result

    # Try Spotify first (if configured)
    if SPOTIFY_CLIENT_ID:
        sp = await _spotify_search(query)
        if sp:
            # Use Spotify metadata, stream via yt-dlp
            ytq = sp.get("search_query", sp["title"])
            yt  = await loop.run_in_executor(
                None, lambda: _ydl_search_one(ytq)
            )
            if yt:
                # Merge: keep Spotify thumbnail & metadata, use yt-dlp URL
                sp["webpage_url"] = yt["webpage_url"]
                sp.pop("search_query", None)
                return sp

    # Try JioSaavn
    js = await _jiosaavn_search(query)
    if js:
        direct = js.get("direct_url", "")
        if direct:
            js["webpage_url"] = direct
            js.pop("direct_url", None)
            js.pop("search_query", None)
            return js
        # Else: search yt-dlp with JioSaavn title
        ytq = js.get("search_query", js["title"])
        yt  = await loop.run_in_executor(None, lambda: _ydl_search_one(ytq))
        if yt:
            js["webpage_url"] = yt["webpage_url"]
            if not js.get("thumbnail"):
                js["thumbnail"] = yt["thumbnail"]
            js.pop("direct_url", None)
            js.pop("search_query", None)
            return js

    # YouTube + SoundCloud via yt-dlp
    result = await loop.run_in_executor(None, lambda: _ydl_search_one(query))
    return result


async def smart_search_multi(query: str, count: int = 6) -> list[dict]:
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, lambda: _ydl_search_multi(query, count))
    return results

# ══════════════════════════════════════════════════════════════
#  LYRICS
# ══════════════════════════════════════════════════════════════

async def fetch_lyrics(artist: str, title: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.lyrics.ovh/v1/{artist}/{title}")
            if r.status_code == 200:
                return r.json().get("lyrics", "").strip() or None
    except Exception as e:
        log.warning("Lyrics fetch failed: %s", e)
    return None

# ══════════════════════════════════════════════════════════════
#  AUTO-LEAVE
# ══════════════════════════════════════════════════════════════

async def _cancel_auto_leave(chat_id: int) -> None:
    t = auto_leave_tasks.pop(chat_id, None)
    if t and not t.done():
        t.cancel()


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

# ══════════════════════════════════════════════════════════════
#  ASSISTANT AUTO-JOIN GROUP
# ══════════════════════════════════════════════════════════════

async def _ensure_in_group(chat_id: int) -> bool:
    """Ensure assistant is a member of the group. Auto-join if not."""
    me = await assistant.get_me()

    # Check membership
    try:
        member = await assistant.get_chat_member(chat_id, me.id)
        status = str(getattr(member, "status", "")).lower()
        if "kicked" in status or "banned" in status:
            log.error("Assistant is banned in chat %d", chat_id)
            return False
        log.info("Assistant already in group %d", chat_id)
        return True
    except Exception as e:
        if "USER_NOT_PARTICIPANT" not in str(e) and "user_not_participant" not in str(e).lower():
            log.info("Membership check for %d: %s — will try to join", chat_id, e)

    # Try to join
    chat_obj = None
    try:
        chat_obj = await assistant.get_chat(chat_id)
    except Exception as e:
        log.warning("get_chat(%d) failed: %s", chat_id, e)
        try:
            chat_obj = await assistant.get_chat(str(chat_id))
        except Exception as e2:
            log.error("Cannot fetch chat %d: %s", chat_id, e2)

    if chat_obj:
        username   = getattr(chat_obj, "username", None)
        invite_link = getattr(chat_obj, "invite_link", None)
        target = username or invite_link

        if target:
            try:
                await assistant.join_chat(target)
                log.info("✅ Assistant joined group %d via %s", chat_id, target)
                await asyncio.sleep(1)  # Let Telegram process membership
                return True
            except Exception as exc:
                if "ALREADY_PARTICIPANT" in str(exc) or "already" in str(exc).lower():
                    log.info("Assistant already in group %d", chat_id)
                    return True
                log.error("Auto-join chat %d failed: %s", chat_id, exc)
                return False

    log.error(
        "Cannot auto-join chat %d — no username or invite link. "
        "Please add the assistant account manually.", chat_id
    )
    return False

# ══════════════════════════════════════════════════════════════
#  PLAYBACK
# ══════════════════════════════════════════════════════════════

async def _do_play(chat_id: int, track: dict) -> bool:
    loop = asyncio.get_running_loop()

    # Step 1: Ensure assistant is in the group
    if not await _ensure_in_group(chat_id):
        return False

    # Step 2: Get stream URL
    # If track already has a direct_url (JioSaavn), use it directly
    webpage = track.get("webpage_url", "")
    if not webpage:
        log.error("No webpage_url for track: %s", track.get("title"))
        return False

    log.info("Fetching stream URL for: %s", track["title"])
    stream_url, thumb = await loop.run_in_executor(None, _get_stream_url, webpage)
    if not stream_url:
        log.error("No stream URL: %s", track["title"])
        return False
    if thumb:
        track["thumbnail"] = thumb

    # Step 3: Build MediaStream
    try:
        stream = MediaStream(
            stream_url,
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )
    except Exception as exc:
        log.error("MediaStream error: %s", exc)
        return False

    # Step 4: Play — auto_start creates VC if needed, join_as = assistant
    config = GroupCallConfig(
        auto_start=True,
        join_as=_assistant_peer,
    )
    try:
        await call.play(chat_id, stream, config)
        log.info("▶️  Playing [%s] %s in chat %d", track.get("source","?"), track["title"], chat_id)

        # Unmute assistant
        try:
            await call.unmute(chat_id)
        except Exception:
            pass

        return True
    except Exception as exc:
        err = str(exc)
        log.error("call.play() failed in %d: %s", chat_id, exc)
        if "NoActiveGroupCall" in err:
            log.error("VC could not be auto-created in chat %d. Grant admin permissions.", chat_id)
        elif "PARTICIPANT_JOIN_MISSING" in err:
            log.error("Assistant not in group %d.", chat_id)
        return False


async def play_next(chat_id: int) -> bool:
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

# ══════════════════════════════════════════════════════════════
#  STREAM END  (auto next song)
# ══════════════════════════════════════════════════════════════

@call.on_update(filters.stream_end())
async def on_stream_end(client: PyTgCalls, update: StreamEnded) -> None:
    if update.stream_type != StreamEnded.Type.AUDIO:
        return

    chat_id: int = update.chat_id
    log.info("Stream ended in chat %d", chat_id)

    current = currently_playing.get(chat_id)
    if current and loop_mode[chat_id]:
        queues[chat_id].insert(0, {k: v for k, v in current.items()})

    currently_playing.pop(chat_id, None)

    if queues.get(chat_id):
        started = await play_next(chat_id)
        if started and _bot_app:
            now = currently_playing.get(chat_id)
            if now:
                try:
                    await _send_now_playing(_bot_app.bot, chat_id, now)
                except Exception as e:
                    log.error("send_now_playing error: %s", e)
        if not started:
            await _schedule_auto_leave(chat_id)
    else:
        await _schedule_auto_leave(chat_id)

# ══════════════════════════════════════════════════════════════
#  UI MESSAGES
# ══════════════════════════════════════════════════════════════

async def _send_now_playing(bot, chat_id: int, track: dict) -> None:
    icon = _platform_icon(track.get("source", ""))
    dur  = _fmt(track.get("duration", 0))
    q_len = len(queues[chat_id])

    text = (
        f"{'━' * 28}\n"
        f"{icon}  **Now Playing**\n\n"
        f"🎵  **{_esc(track['title'])}**\n"
        f"👤  {_esc(track.get('uploader', 'Unknown'))}\n"
        f"⏱  `{dur}`\n"
        f"📻  {track.get('source', 'Unknown')}\n"
        f"📋  {q_len} track{'s' if q_len != 1 else ''} in queue\n"
        f"{'━' * 28}"
    )
    kb = player_kb(chat_id)
    thumb = track.get("thumbnail", "")
    sent = False
    if thumb:
        try:
            await bot.send_photo(
                chat_id=chat_id, photo=thumb,
                caption=text, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb,
            )
            sent = True
        except Exception:
            pass
    if not sent:
        await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
        )

# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

HELP_TEXT = """
*🎵 ZenixMusic — Commands*

`/play <song or URL>` — Play from YouTube, SoundCloud, Spotify, JioSaavn, etc\\.
`/search <query>` — Browse top results and choose
`/np` — Show now playing
`/queue` — View current queue
`/skip` — Skip current track _(admin)_
`/pause` — Pause playback
`/resume` — Resume playback
`/stop` — Stop and clear queue _(admin)_
`/loop` — Toggle loop mode
`/shuffle` — Shuffle the queue
`/remove <pos>` — Remove track from queue
`/lyrics [query]` — Get lyrics
`/ping` — Check bot latency
`/logs` — View recent logs _(admin)_
`/help` — Show this message
"""


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "there"
    text = (
        f"*Hey {_esc(name)}\\! 👋*\n\n"
        f"I'm **ZenixMusic** — your premium Telegram music bot\\.\n\n"
        f"🎵 Play music from *YouTube, SoundCloud, Spotify, JioSaavn* and more\\.\n"
        f"🔊 Crystal clear audio in Voice Chat\\.\n"
        f"⚡ Fast search across all platforms\\.\n\n"
        f"Use /play to get started or /help for all commands\\."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true"),
        InlineKeyboardButton("❓ Help", callback_data="help"),
    ]])
    try:
        await update.message.reply_video(
            video="https://files.catbox.moe/9w0qsn.mp4",
            caption=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb,
        )
    except Exception:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import time
    t = time.monotonic()
    msg = await update.message.reply_text("🏓 Pinging\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    ms = round((time.monotonic() - t) * 1000)
    await msg.edit_text(f"🏓 Pong\\!  `{ms}ms`", parse_mode=ParseMode.MARKDOWN_V2)


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Usage: `/play <song name or URL>`\n\nSupports YouTube, SoundCloud, Spotify, JioSaavn\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query = " ".join(context.args)
    msg   = await update.message.reply_text(
        f"🔍  Searching for `{_esc(query)}`\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    track = await smart_search(query)
    if not track:
        await msg.edit_text(
            "❌  No results found\\.\n\nTry a different query or check if the URL is valid\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    queues[chat_id].append(track)
    await _cancel_auto_leave(chat_id)
    await _safe_delete(msg)

    if currently_playing.get(chat_id) is None:
        loading = await context.bot.send_message(
            chat_id,
            f"⏳  Loading *{_esc(track['title'])}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        started = await play_next(chat_id)
        await _safe_delete(loading)

        if started:
            now = currently_playing.get(chat_id, track)
            await _send_now_playing(context.bot, chat_id, now)
        else:
            await context.bot.send_message(
                chat_id,
                "❌  Playback failed\\.\n\n"
                "Make sure:\n"
                "• The assistant account is added to this group\n"
                "• Bot has *Manage Voice Chats* permission\n"
                "• A Voice Chat is active or bot can create one",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    else:
        pos  = len(queues[chat_id])
        icon = _platform_icon(track.get("source", ""))
        caption = (
            f"✅  *Added to Queue* \\#{pos}\n\n"
            f"{icon}  *{_esc(track['title'])}*\n"
            f"👤  {_esc(track.get('uploader', 'Unknown'))}\n"
            f"⏱  `{_fmt(track.get('duration', 0))}`\n"
            f"📻  {track.get('source', 'Unknown')}"
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
    msg   = await update.message.reply_text(
        f"🔍  Searching: `{_esc(query)}`\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    loop = asyncio.get_running_loop()
    results = await smart_search_multi(query, 6)

    if not results:
        await msg.edit_text("❌  No results found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    cache: dict = context.bot_data.setdefault(SEARCH_CACHE_KEY, {})
    cache[msg.message_id] = results

    # Build result list text
    text = f"🎵  *Search Results for:* `{_esc(query)}`\n{'━'*28}\n\n"
    for i, r in enumerate(results):
        icon = _platform_icon(r.get("source", ""))
        text += (
            f"{icon}  `{i+1}.`  *{_esc(r['title'][:50])}*\n"
            f"      👤 {_esc(r.get('uploader','?'))}   ⏱ `{_fmt(r['duration'])}`\n\n"
        )

    buttons = [
        [InlineKeyboardButton(
            f"{_platform_icon(r.get('source',''))} {i+1}. {r['title'][:35]} [{_fmt(r['duration'])}]",
            callback_data=f"sel:{msg.message_id}:{i}",
        )]
        for i, r in enumerate(results)
    ]
    buttons.append([InlineKeyboardButton("✖  Cancel", callback_data="cancel_search")])

    await msg.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def np_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = currently_playing.get(chat_id)
    if not now:
        await update.message.reply_text("⏸  Nothing is playing right now\\.\n\nUse /play to start\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await _send_now_playing(context.bot, chat_id, now)


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = currently_playing.get(chat_id)
    q   = queues[chat_id]

    if not now and not q:
        await update.message.reply_text("📋  Queue is empty\\.\n\nUse /play to add songs\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    total = sum(t.get("duration", 0) for t in q)
    text  = f"📋  *Music Queue*\n{'━'*28}\n\n"

    if now:
        icon = _platform_icon(now.get("source", ""))
        text += f"▶️  *Now Playing*\n{icon}  {_esc(now['title'][:50])}\n`{_fmt(now.get('duration',0))}`\n\n"

    if q:
        text += f"*Up Next:*\n"
        for i, t in enumerate(q[:12], 1):
            icon = _platform_icon(t.get("source", ""))
            text += f"`{i}.`  {icon}  {_esc(t['title'][:45])}  `{_fmt(t.get('duration',0))}`\n"
        if len(q) > 12:
            text += f"\n_\\.\\.\\. and {len(q)-12} more_\n"
        text += f"\n⏳  Total: `{_fmt(total)}`"

    if loop_mode[chat_id]:
        text += "\n🔁  Loop mode is *ON*"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.pause(update.effective_chat.id)
        await update.message.reply_text("⏸  Playback paused\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text("⚠️  Nothing is playing\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await call.resume(update.effective_chat.id)
        await update.message.reply_text("▶️  Playback resumed\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text("⚠️  Nothing is paused\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("🔒  Only admins can skip\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    chat_id = update.effective_chat.id
    if not currently_playing.get(chat_id):
        await update.message.reply_text("⚠️  Nothing to skip\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await update.message.reply_text("⏭  Skipping\\.\\.\\.  ", parse_mode=ParseMode.MARKDOWN_V2)

    if queues[chat_id]:
        started = await play_next(chat_id)
        if started:
            now = currently_playing.get(chat_id)
            if now:
                await _send_now_playing(context.bot, chat_id, now)
    else:
        currently_playing.pop(chat_id, None)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        await context.bot.send_message(chat_id, "✅  Queue is empty\\.\nUse /play to add more songs\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("🔒  Only admins can stop playback\\.", parse_mode=ParseMode.MARKDOWN_V2)
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
    await update.message.reply_text("⏹  Playback stopped and queue cleared\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    loop_mode[chat_id] = not loop_mode[chat_id]
    state = "ON 🔁" if loop_mode[chat_id] else "OFF ▶️"
    await update.message.reply_text(f"🔁  Loop mode: *{state}*", parse_mode=ParseMode.MARKDOWN_V2)


async def shuffle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    q = queues[chat_id]
    if not q:
        await update.message.reply_text("📋  Queue is empty\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    random.shuffle(q)
    await update.message.reply_text(f"🔀  Shuffled *{len(q)}* tracks\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/remove <position>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    pos = int(context.args[0])
    q   = queues[chat_id]
    if not q:
        await update.message.reply_text("📋  Queue is empty\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    if pos < 1 or pos > len(q):
        await update.message.reply_text(f"⚠️  Invalid position\\. Queue has *{len(q)}* tracks\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    removed = q.pop(pos - 1)
    icon = _platform_icon(removed.get("source", ""))
    await update.message.reply_text(
        f"🗑  Removed: {icon}  *{_esc(removed['title'])}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if context.args:
        query = " ".join(context.args)
        if " - " in query:
            artist, title = query.split(" - ", 1)
        else:
            now    = currently_playing.get(chat_id)
            artist = (now or {}).get("uploader", "Unknown")
            title  = query
    else:
        now = currently_playing.get(chat_id)
        if not now:
            await update.message.reply_text(
                "Usage: `/lyrics Artist \\- Song Title`\nor just `/lyrics Song Title` while playing\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        artist = now.get("uploader", "Unknown")
        title  = now.get("title", "Unknown")

    msg    = await update.message.reply_text(f"📝  Fetching lyrics for *{_esc(title)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    lyrics = await fetch_lyrics(artist.strip(), title.strip())
    if not lyrics:
        await msg.edit_text(
            f"❌  Lyrics not found for *{_esc(title)}*\\.\n\nTry: `/lyrics Artist \\- Song Title`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    snippet = lyrics[:3800] + ("\n\n_\\[truncated\\]_" if len(lyrics) > 3800 else "")
    await msg.edit_text(f"📝  *{_esc(title)}*\n\n{_esc(snippet)}", parse_mode=ParseMode.MARKDOWN_V2)


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("🔒  Admin only\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    lines = list(_log_handler.buffer)
    if not lines:
        await update.message.reply_text("📋  No logs yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    full = "\n".join(lines)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await update.message.reply_text(f"```\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN_V2)

# ══════════════════════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q       = update.callback_query
    chat_id = q.message.chat.id
    data    = q.data
    await q.answer()

    # Help button
    if data == "help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Search result selection
    if data.startswith("sel:"):
        _, mid_s, idx_s = data.split(":", 2)
        mid, idx = int(mid_s), int(idx_s)
        cache   = context.bot_data.get(SEARCH_CACHE_KEY, {})
        results = cache.get(mid)
        if not results or idx >= len(results):
            await q.edit_message_text("⚠️  Results expired\\. Search again\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        track = results[idx]
        cache.pop(mid, None)
        queues[chat_id].append(track)
        await _cancel_auto_leave(chat_id)
        await _safe_delete(q.message)

        if currently_playing.get(chat_id) is None:
            lm = await context.bot.send_message(chat_id, f"⏳  Loading *{_esc(track['title'])}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
            started = await play_next(chat_id)
            await _safe_delete(lm)
            if started:
                now = currently_playing.get(chat_id, track)
                await _send_now_playing(context.bot, chat_id, now)
            else:
                await context.bot.send_message(chat_id, "❌  Playback failed\\. Check VC permissions\\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            pos = len(queues[chat_id])
            await context.bot.send_message(
                chat_id,
                f"✅  Added *{_esc(track['title'])}* to queue at \\#{pos}\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    if data == "cancel_search":
        await _safe_delete(q.message)
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
            await q.edit_message_reply_markup(player_kb(chat_id))
        except Exception:
            pass

    elif data == "shuffle":
        qq = queues[chat_id]
        if qq:
            random.shuffle(qq)
            await q.answer(f"🔀 Shuffled {len(qq)} tracks!", show_alert=False)
        else:
            await q.answer("Queue is empty!", show_alert=False)

    elif data == "np":
        now = currently_playing.get(chat_id)
        if now:
            await _send_now_playing(context.bot, chat_id, now)
        else:
            await q.answer("Nothing is playing!", show_alert=False)

    elif data == "skip":
        if not currently_playing.get(chat_id):
            return
        if queues[chat_id]:
            started = await play_next(chat_id)
            if started:
                now = currently_playing.get(chat_id)
                if now:
                    await _send_now_playing(context.bot, chat_id, now)
        else:
            currently_playing.pop(chat_id, None)
            try:
                await call.leave_call(chat_id)
            except Exception:
                pass
            await context.bot.send_message(chat_id, "✅  Queue finished\\. Add more with /play\\.", parse_mode=ParseMode.MARKDOWN_V2)

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        loop_mode[chat_id] = False
        await _cancel_auto_leave(chat_id)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        await q.edit_message_text("⏹  Playback stopped\\.", parse_mode=ParseMode.MARKDOWN_V2)

    elif data == "queue":
        now = currently_playing.get(chat_id)
        qq  = queues[chat_id]
        if not now and not qq:
            await q.answer("Queue is empty!", show_alert=False)
            return
        lines = []
        if now:
            lines.append(f"▶️ {now['title'][:45]}")
        for i, t in enumerate(qq[:8], 1):
            icon = _platform_icon(t.get("source",""))
            lines.append(f"{i}. {icon} {t['title'][:40]}")
        if len(qq) > 8:
            lines.append(f"...+{len(qq)-8} more")
        await context.bot.send_message(chat_id, "\n".join(lines))

# ══════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ══════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update error:", exc_info=context.error)

# ══════════════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ══════════════════════════════════════════════════════════════

async def post_init(application: Application) -> None:
    global _bot_app, _assistant_peer

    _bot_app = application
    await application.bot.delete_webhook(drop_pending_updates=True)
    log.info("Webhook cleared")

    log.info("Starting assistant (pyrofork)...")
    await assistant.start()
    me = await assistant.get_me()
    log.info("Assistant: %s  (ID: %d)", me.first_name, me.id)

    try:
        _assistant_peer = await assistant.resolve_peer(me.id)
        log.info("Assistant peer resolved for join_as")
    except Exception as e:
        log.warning("Could not resolve assistant peer: %s", e)

    # Refresh Spotify token if configured
    if SPOTIFY_CLIENT_ID:
        await _refresh_spotify_token()

    log.info("Starting PyTgCalls...")
    await call.start()
    log.info("🎵 ZenixMusic v5.0 is live!")


async def post_shutdown(application: Application) -> None:
    log.info("Shutting down...")
    for t in auto_leave_tasks.values():
        t.cancel()
    try:
        await assistant.stop()
    except Exception:
        pass
    log.info("Goodbye!")

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING not set")
    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID and API_HASH must be set")

    threading.Thread(target=_run_health_server, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    for cmd, fn in [
        ("start",   start_cmd),
        ("help",    help_cmd),
        ("ping",    ping_cmd),
        ("play",    play_cmd),
        ("search",  search_cmd),
        ("np",      np_cmd),
        ("queue",   queue_cmd),
        ("pause",   pause_cmd),
        ("resume",  resume_cmd),
        ("skip",    skip_cmd),
        ("stop",    stop_cmd),
        ("loop",    loop_cmd),
        ("shuffle", shuffle_cmd),
        ("remove",  remove_cmd),
        ("lyrics",  lyrics_cmd),
        ("logs",    logs_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    log.info("Polling started...")
    app.run_polling(drop_pending_updates=True, stop_signals=None)
    register_bgm_handlers(app)

if __name__ == "__main__":
    main()
