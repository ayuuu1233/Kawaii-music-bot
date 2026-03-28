"""
🎀 Kawaii Telegram Music Bot
Built with Pyrogram + PyTgCalls + yt-dlp
"""        



import asyncio
import os
import re
from collections import defaultdict
from typing import Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls.types import AudioQuality

# ─── CONFIG ────────────────────────────────────────────────────────────────────
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")  # Pyrogram session string for assistant

# ─── CLIENTS ───────────────────────────────────────────────────────────────────
bot = Client("music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
call = PyTgCalls(assistant)

#----- 🎀typeing -----------------------------------------
async def typing_effect(message, text: str):
    typed = ""
    for ch in text:
        typed += ch
        try:
            await msg.edit_text(typed)
        except:
            pass
        await asyncio.sleep(0.04)

# ─── STATE ─────────────────────────────────────────────────────────────────────
# queues[chat_id] = [{"title": ..., "url": ...}, ...]
queues: dict[int, list[dict]] = defaultdict(list)
# currently_playing[chat_id] = {"title": ..., "url": ...}
currently_playing: dict[int, Optional[dict]] = {}

# ─── INLINE KEYBOARD ───────────────────────────────────────────────────────────
def player_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            InlineKeyboardButton("⏭️ Skip", callback_data="skip"),
        ],
        [
            InlineKeyboardButton("⏹️ Stop", callback_data="stop"),
            InlineKeyboardButton("📋 Queue", callback_data="queue"),
        ],
    ])

# ─── YT-DLP HELPER ─────────────────────────────────────────────────────────────
def search_yt(query: str) -> Optional[dict]:
    """Search YouTube and return best audio stream info."""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "source_address": "0.0.0.0",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "Unknown"),
                "url": info.get("url"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", ""),
            }
    except Exception as e:
        print(f"[yt-dlp error] {e}")
        return None

# ─── PLAYBACK HELPERS ──────────────────────────────────────────────────────────
async def play_next(chat_id: int):
    """Pop next track from queue and start streaming."""
    if not queues[chat_id]:
        currently_playing.pop(chat_id, None)
        return

    track = queues[chat_id].pop(0)
    currently_playing[chat_id] = track

    try:
        await call.play(
            chat_id,
            MediaStream(
                track["url"],
                audio_quality=AudioQuality.STUDIO,
            ),
        )
    except Exception as e:
        print(f"[play error] {e}")
        currently_playing.pop(chat_id, None)


async def ensure_in_voice(chat_id: int, message: Message):
    """Join the voice chat if not already in it."""
    try:
        await call.join_group_call(
            chat_id,
            MediaStream("", audio_quality=AudioQuality.STUDIO),   # dummy — replaced on play
        )
    except Exception:
        pass   # Already joined or will join on play()


# ─── COMMANDS ──────────────────────────────────────────────────────────────────
@bot.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    name = message.from_user.first_name if message.from_user else "Senpai"

    # Step 1: Initial message
    msg = await message.reply_text("💖 Starting... UwU")

    # Step 2: Typing animation
    await typing_effect(msg, "✨ Loading Kawaii Music System... 🎶")

    await asyncio.sleep(1)

    await typing_effect(msg, "💫 Connecting to Voice Engine... 🎧")

    await asyncio.sleep(1)

    await typing_effect(msg, f"🌸 Welcome {name}-senpai... UwU 💕")

    await asyncio.sleep(1)

    # Step 3: Send video intro
    await message.reply_video(
        video="https://files.catbox.moe/9w0qsn.mp4",
        caption=(
        f"Konnichiwa **{name}**-senpai~! 💖\n\n"
        "I'm your kawaii music bot, here to fill your voice chat with sweet tunes! 🎵\n\n"
        "**Commands:**\n"
        "🎶 /play `<song>` — Play a song\n"
        "⏸️ /pause — Pause music\n"
        "▶️ /resume — Resume music\n"
        "⏭️ /skip — Skip current track\n"
        "⏹️ /stop — Stop and clear queue\n"
        "📋 /queue — Show queue\n\n"
        "Let's make some music magic, nya~! ✨"
    )
    await message.reply_text(text)


@bot.on_message(filters.command("play") & filters.group)
async def play_cmd(_, message: Message):
    chat_id = message.chat.id

    if len(message.command) < 2:
        await message.reply_text("Ara ara~ tell me what to play Senpai! 🎵\nUsage: `/play <song name>`")
        return

    query = " ".join(message.command[1:])
    searching_msg = await message.reply_text(f"🔍 Searching for **{query}**... please wait nya~")

    track = await asyncio.get_event_loop().run_in_executor(None, search_yt, query)

    if not track:
        await searching_msg.edit_text("Gomen nasai~ I couldn't find that song 😢")
        return

    queues[chat_id].append(track)

    # If nothing is playing, start immediately
    if chat_id not in currently_playing or currently_playing.get(chat_id) is None:
        await searching_msg.delete()
        await play_next(chat_id)
        track = currently_playing.get(chat_id, track)
        await message.reply_text(
            f"🎶 Playing your song, Senpai~\n\n"
            f"**{track['title']}**",
            reply_markup=player_keyboard(),
        )
    else:
        pos = len(queues[chat_id])
        await searching_msg.edit_text(
            f"✅ Added to queue at position **#{pos}**!\n\n"
            f"**{track['title']}**\n\n"
            "Be patient Senpai, it'll be your turn soon~ 🌸"
        )


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, message: Message):
    try:
        await call.pause_stream(message.chat.id)
        await message.reply_text("Music paused UwU ⏸️\nSay /resume when you're ready~")
    except Exception:
        await message.reply_text("Hmm~ nothing is playing right now Senpai! 🤔")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, message: Message):
    try:
        await call.resume_stream(message.chat.id)
        await message.reply_text("Yay! Music is back~! ▶️🎶")
    except Exception:
        await message.reply_text("Hmm~ nothing is paused right now Senpai! 🤔")


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, message: Message):
    chat_id = message.chat.id
    if chat_id not in currently_playing or currently_playing.get(chat_id) is None:
        await message.reply_text("There's nothing to skip nya~ 🌸")
        return

    await message.reply_text("Skipping this track, hehe~ ➡️")
    try:
        await call.leave_group_call(chat_id)
    except Exception:
        pass
    currently_playing.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
        track = currently_playing.get(chat_id)
        if track:
            await message.reply_text(
                f"Now playing~ 🎵\n\n**{track['title']}**",
                reply_markup=player_keyboard(),
            )
    else:
        await message.reply_text("The queue is empty now~ 🌸 Add more songs with /play!")


@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, message: Message):
    chat_id = message.chat.id
    queues[chat_id].clear()
    currently_playing.pop(chat_id, None)
    try:
        await call.leave_group_call(chat_id)
    except Exception:
        pass
    await message.reply_text("Music stopped! Bye bye~ 👋\nHope you enjoyed it Senpai 💖")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, message: Message):
    chat_id = message.chat.id
    now = currently_playing.get(chat_id)
    q = queues[chat_id]

    if not now and not q:
        await message.reply_text("The queue is empty nya~ 🌸\nUse /play to add songs!")
        return

    text = "🎵 **Music Queue** 🎵\n\n"
    if now:
        text += f"▶️ **Now Playing:**\n{now['title']}\n\n"
    if q:
        text += "📋 **Up Next:**\n"
        for i, track in enumerate(q, 1):
            text += f"{i}. {track['title']}\n"

    await message.reply_text(text)


# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────
@bot.on_callback_query()
async def callback_handler(_, query):
    chat_id = query.message.chat.id
    data = query.data

    if data == "pause":
        try:
            await call.pause_stream(chat_id)
            await query.answer("Music paused UwU ⏸️")
        except Exception:
            await query.answer("Nothing is playing~")

    elif data == "resume":
        try:
            await call.resume_stream(chat_id)
            await query.answer("Music resumed~ ▶️🎶")
        except Exception:
            await query.answer("Nothing is paused~")

    elif data == "skip":
        if currently_playing.get(chat_id) is None:
            await query.answer("Nothing to skip~")
            return
        await query.answer("Skipping this track, hehe~ ➡️")
        try:
            await call.leave_group_call(chat_id)
        except Exception:
            pass
        currently_playing.pop(chat_id, None)
        if queues[chat_id]:
            await play_next(chat_id)
            track = currently_playing.get(chat_id)
            if track:
                await query.message.reply_text(
                    f"Now playing~ 🎵\n\n**{track['title']}**",
                    reply_markup=player_keyboard(),
                )

    elif data == "stop":
        queues[chat_id].clear()
        currently_playing.pop(chat_id, None)
        try:
            await call.leave_group_call(chat_id)
        except Exception:
            pass
        await query.answer("Stopped! Bye bye~ 👋")
        await query.message.edit_text("Music stopped! Hope you enjoyed it Senpai 💖")

    elif data == "queue":
        now = currently_playing.get(chat_id)
        q = queues[chat_id]
        if not now and not q:
            await query.answer("Queue is empty~")
            return
        text = ""
        if now:
            text += f"▶️ Now: {now['title']}\n"
        for i, t in enumerate(q, 1):
            text += f"{i}. {t['title']}\n"
        await query.answer(text[:200], show_alert=True)


# ─── STREAM END HANDLER ────────────────────────────────────────────────────────
@call.on_stream_end()
async def on_stream_end(client, update):
    chat_id = update.chat_id
    currently_playing.pop(chat_id, None)
    if queues[chat_id]:
        await play_next(chat_id)


# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    print("Starting assistant client...")
    await assistant.start()
    print("Starting call handler...")
    await call.start()
    print("Starting bot...")
    await bot.start()
    print("🎀 Kawaii Music Bot is running~ nyaa!")
    await asyncio.get_event_loop().create_future()  # run forever


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Bot stopped. Bye bye~ 👋")
