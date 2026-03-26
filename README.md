# Zoom Class Auto-Recorder Bot

A Telegram bot that automatically joins Zoom meetings, records them via OBS, and stops recording when the meeting ends.

## How It Works

1. Send a Zoom link or meeting ID + password to the Telegram bot
2. Bot opens Zoom, joins the meeting, switches OBS to the recording scene, and starts recording
3. When the meeting ends, bot automatically stops recording and notifies you on Telegram

## Prerequisites

- macOS
- [Zoom](https://zoom.us/download) installed
- [OBS Studio](https://obsproject.com/download) installed (v28+ with built-in WebSocket)
- [Python 3.10+](https://www.python.org/downloads/)
- A Telegram account

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/edw11/bot.git
cd bot
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token you receive

### 4. Get your Telegram user ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your numeric user ID

### 5. Configure OBS WebSocket

1. Open OBS Studio
2. Go to `Tools > WebSocket Server Settings`
3. Enable the WebSocket server
4. Set a password (or note the auto-generated one)
5. Default port is `4455`

### 6. Set up OBS scene (one-time)

1. In OBS, create a new scene called **"Zoom Recording"**
2. Add a **Display Capture** source — select your MacBook's built-in display
3. Add an **Audio Output Capture** source — to capture system audio
4. Resize the display capture to fill the canvas
5. In `Settings > Video`, set the canvas and output resolution to match your display (e.g., 1440x900)

### 7. Configure Zoom (recommended)

Open Zoom settings and set:
- **Video > Turn off my video when joining a meeting** (checked)
- **Audio > Mute my mic when joining a meeting** (checked)
- **General > Always show this preview dialog when joining** (unchecked)

This lets the bot join meetings directly without manual interaction.

### 8. Create the `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_USER_ID=your_user_id_here
OBS_WS_PORT=4455
OBS_WS_PASSWORD=your_obs_websocket_password
```

### 9. Run the bot

```bash
python3 bot.py
```

## Usage

Send any of these to your Telegram bot:

| Format | Example |
|--------|---------|
| Zoom link | `https://zoom.us/j/123456789?pwd=xxx` |
| Command | `/join 123456789 password` |
| Meeting info | `Meeting ID: 123 456 789 Passcode: xxx` |

### Commands

- `/start` — Show help
- `/join <id> <password>` — Join meeting and start recording
- `/stop` — Manually stop recording
- `/status` — Check if recording is active

## How Meeting End Detection Works

The bot monitors Zoom's `aomhost` and `CptHost` helper processes which only exist during an active meeting. When these processes disappear (after the host ends the meeting), the bot waits for 4 consecutive checks (12 seconds) to confirm, then stops OBS recording and sends a Telegram notification.
