import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped
from pytgcalls.types import StreamAudioEnded
import yt_dlp


API_ID = 21621475
API_HASH = "50c4947b6fe96901599c8b18b09f3e13"
BOT_TOKEN = "7924287783:AAF8fh-HJp1nNgecaz9pf9K-gG514aA5_b0"
SESSION = "BQFJ6uMAwP9Eovt9pF_qPWVfSR3o8DPppNraZWSledX0QJMxDSKc8qPur7Ewj9HtQZc0xsIYm1m04jhohAJEUrCsG0EkDBQDrUCxTCNmxZr13BnyiN7jIZRRkyQiG_ggt4tgOgxS6RQAGAHW4jhDI9kNE3xkbylK4aSBQ_43Jh2ynZS18RPf3LEBDjm-gCiFx8GaqvxrEZlIpY7Zz6RJSgoMmX9YNE4y0fWN5Z3C8OLubFVFI2j74hjvFy2pVAo3o-TJBsv30Cbt4eAlIXqDxijdyNCU7xUUy1ne3fYOIRxHHSKtVGZSFyJyuyPBQprutfR1BzIyx5qVT1ZM_G9UteD43Zh5jwAAAAEzcQmrAA"

app = Client("musicbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
# ✅ ADD HERE
assistant = Client(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION
)

# ✅ CHANGE HERE
call = PyTgCalls(assistant)

# 🎀 Typing animation
async def typing_effect(message, text):
    for i in range(1, len(text) + 1):
        try:
            await message.edit_text(text[:i])
        except:
            pass
        await asyncio.sleep(0.04)


# 🎧 Queue system
QUEUE = {}

# 🔍 YouTube Search
def yt_search(query):
    ydl_opts = {
        "format": "bestaudio",
        "quiet": True,
        "noplaylist": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch:{query}", download=False)["entries"][0]
        return info["url"], info["title"]

# 🎵 Start Stream
async def start_stream(chat_id: int):
    if chat_id not in QUEUE or not QUEUE[chat_id]:
        return

    url, title = QUEUE[chat_id][0]

    # New AudioPiped import
    from pytgcalls import AudioPiped

    # Join the VC and start streaming
    await call.join_group_call(
        chat_id,
        AudioPiped(url),
        stream_type="local_stream"  # new API requires stream_type
    )

    print(f"Now playing: {title} 🎶")


 # 💖 START COMMAND
@app.on_message(filters.command("start"))
async def start(_, message):
    msg = await message.reply_text("💖 Starting... UwU")

    await typing_effect(
        msg,
        "✨ Loading Kawaii Music System... 🎶\n🌸 Preparing everything for you Senpai~ 💕"
    )

    await message.reply_video(
        video="https://files.catbox.moe/9w0qsn.mp4",
        caption=(
            "💖 *Kawaii Music Bot Activated!* 🎶\n\n"
            f"🌸 Hello {message.from_user.first_name} Senpai~ UwU 💕\n"
            "🎧 I am your cute music assistant!\n\n"
            "👉 Use: /play <song name>\n"
            "💡 Example: /play Faded"
        )
    )

# ▶️ PLAY
@app.on_message(filters.command("play") & filters.group)
async def play(_, message):
    if len(message.command) < 2:
        return await message.reply("❌ Give song name")

    query = " ".join(message.command[1:])
    url, title = yt_search(query)

    chat_id = message.chat.id

    if chat_id not in QUEUE:
        QUEUE[chat_id] = []

    QUEUE[chat_id].append((url, title))

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏯ Pause", callback_data="pause"),
         InlineKeyboardButton("⏭ Skip", callback_data="skip")],
        [InlineKeyboardButton("⏹ Stop", callback_data="stop")]
    ])

    await message.reply(f"🎧 Added: **{title}**", reply_markup=buttons)

    if len(QUEUE[chat_id]) == 1:
        await start_stream(chat_id)

# ⏭ AUTO NEXT
@call.on_stream_end()
async def on_end(_, update):
    chat_id = update.chat_id
    print(f"Song ended in chat {chat_id} 🔥")

    if chat_id in QUEUE:
        QUEUE[chat_id].pop(0)  # Remove finished song

        if QUEUE[chat_id]:
            await start_stream(chat_id)  # Play next song
        else:
            await call.leave_group_call(chat_id)  # Leave VC
            print(f"Left VC in chat {chat_id} 🎵")

# ⏭ SKIP
@app.on_message(filters.command("skip") & filters.group)
async def skip(_, message):
    chat_id = message.chat.id

    if chat_id in QUEUE and len(QUEUE[chat_id]) > 1:
        QUEUE[chat_id].pop(0)
        await start_stream(chat_id)
        await message.reply("⏭ Skipped!")
    else:
        await message.reply("❌ No more songs")

# ⏹ STOP
@app.on_message(filters.command("stop") & filters.group)
async def stop(_, message):
    chat_id = message.chat.id

    QUEUE[chat_id] = []
    await call.leave_group_call(chat_id)
    await message.reply("⛔ Stopped!")

# ⏸ PAUSE
@app.on_message(filters.command("pause") & filters.group)
async def pause(_, message):
    await call.pause_stream(message.chat.id)
    await message.reply("⏸ Paused")

# ▶️ RESUME
@app.on_message(filters.command("resume") & filters.group)
async def resume(_, message):
    await call.resume_stream(message.chat.id)
    await message.reply("▶️ Resumed")

# 📜 QUEUE
@app.on_message(filters.command("queue") & filters.group)
async def queue(_, message):
    chat_id = message.chat.id

    if chat_id not in QUEUE or not QUEUE[chat_id]:
        return await message.reply("❌ Empty queue")

    text = "🎶 **Queue:**\n\n"
    for i, (_, title) in enumerate(QUEUE[chat_id], start=1):
        text += f"{i}. {title}\n"

    await message.reply(text)

# 🔘 BUTTON HANDLER
@app.on_callback_query()
async def buttons(_, query):
    chat_id = query.message.chat.id

    if query.data == "pause":
        await call.pause_stream(chat_id)
        await query.answer("Paused")

    elif query.data == "skip":
        if chat_id in QUEUE and len(QUEUE[chat_id]) > 1:
            QUEUE[chat_id].pop(0)
            await start_stream(chat_id)
            await query.answer("Skipped")

    elif query.data == "stop":
        QUEUE[chat_id] = []
        await call.leave_group_call(chat_id)
        await query.answer("Stopped")

# 🚀 START
assistant.start()
app.start()
call.start()
print("🔥 GOD MUSIC BOT RUNNING 🔥")
asyncio.get_event_loop().run_forever()
