<div align="center">

# 🎀 Kawaii Music Bot

<img src="https://graph.org/file/9901c2070cea11d1aa194.jpg" width="300"/>

> *Konnichiwa~ Your kawaii Telegram Voice Chat music bot!* 🌸

[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python)](https://python.org)
[![py-tgcalls](https://img.shields.io/badge/py--tgcalls-2.2.11-pink?style=for-the-badge)](https://github.com/pytgcalls/pytgcalls)
[![pyrofork](https://img.shields.io/badge/pyrofork-2.3.69-purple?style=for-the-badge)](https://github.com/Mayuri-Chan/pyrofork)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

</div>

---

## ✨ Features

- 🎵 **Voice Chat Playback** — Telegram group VC mein seedha music bajao
- 🔍 **Smart Search** — YouTube + SoundCloud dono se search
- 📋 **Queue System** — Multiple songs queue karo
- 🖼️ **HD Thumbnails** — Har song ke saath best quality thumbnail
- 🔁 **Loop Mode** — Ek song repeat karo
- 🔀 **Shuffle** — Queue ko shuffle karo
- 🤖 **Auto Group Join** — Assistant automatically group join kar leta hai
- 🚀 **Auto VC Join** — Voice Chat manually start karne ki zaroorat nahi
- 📝 **Lyrics** — Song ke lyrics fetch karo
- ⏸️ **Full Controls** — Pause, Resume, Skip, Stop, Volume
- 🪵 **Live Logs** — Admin ke liye real-time logs
- 💚 **Health Server** — Render/Railway pe alive rakhne ke liye

---

## 🛠️ Requirements

- Python **3.10+**
- `ffmpeg` installed on server
- A **Telegram User Account** (assistant) — VC join karne ke liye
- A **Telegram Bot** — commands receive karne ke liye

---

## 📦 Installation

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/kawaii-music-bot.git
cd kawaii-music-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install ffmpeg

```bash
# Ubuntu/Debian
apt install ffmpeg

# Render/Railway — already available, kuch karne ki zarurat nahi
```

---

## ⚙️ Environment Variables

`.env` file banao ya hosting platform pe set karo:

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=7924287783:AAFLg1cVAaVB5sXHNGxx1_2ZjU5nGEJj1Yk
SESSION_STRING=BQABAAEAAf...
ADMIN_IDS=123456789,987654321
AUTO_LEAVE_SECS=120
PORT=8080
```

| Variable | Description | Required |
|---|---|---|
| `API_ID` | Assistant user account ka API ID ([my.telegram.org](https://my.telegram.org)) | ✅ |
| `API_HASH` | Assistant user account ka API Hash ([my.telegram.org](https://my.telegram.org)) | ✅ |
| `BOT_TOKEN` | Bot token ([BotFather](https://t.me/BotFather)) | ✅ |
| `SESSION_STRING` | Assistant account ka Pyrogram session string | ✅ |
| `ADMIN_IDS` | Admin user IDs (comma separated) | ❌ |
| `AUTO_LEAVE_SECS` | VC se auto-leave timer in seconds (default: 120) | ❌ |
| `PORT` | Health server port (default: 8080) | ❌ |
| `COOKIES_FILE` | YouTube cookies file path (default: cookies.txt) | ❌ |

---

## 🔑 Session String Kaise Banayein

Assistant account ka session string generate karne ke liye:

```python
from pyrogram import Client

api_id = 12345678        # my.telegram.org se
api_hash = "abcdef..."   # my.telegram.org se

with Client("session", api_id=api_id, api_hash=api_hash) as app:
    print(app.export_session_string())
```

> ⚠️ **Note:** `API_ID`, `API_HASH` aur `SESSION_STRING` — teeno **assistant user account** ke hain, bot ke nahi!

---

## 📋 Requirements.txt

```
py-tgcalls==2.2.11
pyrofork==2.3.69
python-telegram-bot==21.6
yt-dlp
httpx
```

---

## 🚀 Deploy on Render

1. [Render.com](https://render.com) pe **New Web Service** banao
2. GitHub repo connect karo
3. Build command:
   ```
   pip install -r requirements.txt
   ```
4. Start command:
   ```
   python kawaii_bot.py
   ```
5. Environment variables set karo (upar wali table dekho)

> ✅ Render pe `ffmpeg` already available hota hai — alag se install karne ki zarurat nahi!

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/start` | Bot ko greet karo |
| `/play <song>` | Song search karke play karo |
| `/search <song>` | Top 5 results mein se choose karo |
| `/np` | Abhi kya chal raha hai |
| `/pause` | Music pause karo |
| `/resume` | Music resume karo |
| `/skip` | Next song pe jaao *(admin only)* |
| `/stop` | Music band karo *(admin only)* |
| `/queue` | Queue dekho |
| `/loop` | Loop mode toggle karo |
| `/shuffle` | Queue shuffle karo |
| `/remove <pos>` | Queue se track hataao |
| `/lyrics [song]` | Song ke lyrics |
| `/logs` | Recent logs *(admin only)* |

---

## 🎮 Inline Controls

Har Now Playing message ke saath inline buttons aate hain:

```
[ ⏸️ Pause ]  [ ▶️ Resume ]  [ ⏭️ Skip ]
[ ⏹️ Stop  ]  [ 📋 Queue  ]  [ 🔁 Loop ]
[          🔀 Shuffle         ]
```

---

## 🏗️ Architecture

```
kawaii_bot.py
│
├── Pyrogram Client (pyrofork)   ← Assistant user account
│   └── Session string se login
│
├── PyTgCalls (py-tgcalls)       ← VC audio streaming
│   ├── call.play()              ← Join VC + play
│   ├── filters.stream_end()     ← Auto next song
│   └── GroupCallConfig          ← join_as assistant
│
├── python-telegram-bot          ← Bot commands
│   ├── CommandHandlers
│   └── CallbackQueryHandler
│
└── yt-dlp                       ← Audio search + stream URL
    ├── YouTube search
    └── SoundCloud search
```

---

## ❓ FAQ

**Q: YouTube "Sign in to confirm" error aa raha hai?**

A: YouTube bot detection hai. SoundCloud se automatically fallback ho jaata hai. Permanent fix ke liye cookies file add karo:
```env
COOKIES_FILE=cookies.txt
```

**Q: Assistant VC mein join nahi ho raha?**

A: Check karo:
- Assistant account group mein add hai?
- Bot ko "Manage Voice Chats" permission di hai?
- `SESSION_STRING` correct hai?

**Q: Private group mein assistant auto-join nahi kar pa raha?**

A: Private groups mein manually assistant ko add karo. Public groups aur invite link wale groups mein auto-join kaam karta hai.

**Q: `API_ID` aur `API_HASH` kiska daalna hai?**

A: **Assistant user account** ka — [my.telegram.org](https://my.telegram.org) pe jaao, apne assistant account se login karo aur wahan se lo. Bot token alag hota hai.

---

## 📜 License

MIT License — freely use, modify, and distribute karo.

---

<div align="center">

Made with 💖 and a lot of nya~

*If this helped you, give it a ⭐ on GitHub!*

</div>
