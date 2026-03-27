import os
import re
import json
import time
import asyncio
import subprocess
import logging
import threading
from datetime import datetime, timedelta

# Fix SSL certificates for macOS Python
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("TELEGRAM_USER_ID", "").split(",") if uid.strip()]
OBS_WS_PORT = int(os.getenv("OBS_WS_PORT", "4455"))
OBS_WS_PASSWORD = os.getenv("OBS_WS_PASSWORD", "")

# Track active recording state
active_session = {"recording": False, "monitor_thread": None}

# Schedule file path
SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.json")


def load_schedule() -> list:
    """Load scheduled classes from JSON file."""
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "r") as f:
            return json.load(f)
    return []


def save_schedule(schedule: list):
    """Save scheduled classes to JSON file."""
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


def is_authorized(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USER_IDS


def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def parse_zoom_info(text: str):
    """Parse Zoom meeting ID and password from various formats.

    Supports:
      /join 123456789 password
      /join 12345678901 password
      https://zoom.us/j/123456789?pwd=xxx
      Meeting ID: 123 456 789 + Passcode: xxx
    """
    # Try URL format
    url_match = re.search(r"zoom\.us/j/(\d+)(?:\?pwd=(\S+))?", text)
    if url_match:
        meeting_id = url_match.group(1)
        password = url_match.group(2) or ""
        # Check if there's a separate passcode in the text
        if not password:
            pwd_match = re.search(r"(?:passcode|password|pwd)[:\s]+(\S+)", text, re.IGNORECASE)
            if pwd_match:
                password = pwd_match.group(1)
        return meeting_id, password

    # Try "Meeting ID: xxx" format
    id_match = re.search(r"(?:meeting\s*id|id)[:\s]+([\d\s]+)", text, re.IGNORECASE)
    if id_match:
        meeting_id = re.sub(r"\s+", "", id_match.group(1))
        pwd_match = re.search(r"(?:passcode|password|pwd)[:\s]+(\S+)", text, re.IGNORECASE)
        password = pwd_match.group(1) if pwd_match else ""
        return meeting_id, password

    # Try simple format: /join <id> <password>
    parts = text.strip().split()
    # Filter out the command if present
    parts = [p for p in parts if not p.startswith("/")]
    if len(parts) >= 2:
        candidate_id = re.sub(r"\s+", "", parts[0])
        if candidate_id.isdigit() and len(candidate_id) >= 9:
            return candidate_id, parts[1]
    if len(parts) == 1:
        candidate_id = re.sub(r"\s+", "", parts[0])
        if candidate_id.isdigit() and len(candidate_id) >= 9:
            return candidate_id, ""

    return None, None


def open_zoom_meeting(meeting_id: str, password: str):
    """Join a Zoom meeting using the zoommtg:// URL scheme."""
    zoom_url = f"zoommtg://zoom.us/join?action=join&confno={meeting_id}"
    if password:
        zoom_url += f"&pwd={password}"
    subprocess.run(["open", zoom_url])



def dismiss_zoom_popups():
    """Dismiss Zoom popups like 'This meeting is being recorded' by clicking Got it/OK."""
    script = '''
    tell application "System Events"
        tell process "zoom.us"
            set frontmost to true
            delay 0.5
            -- Try every window
            repeat with w in (every window)
                try
                    click button "OK" of w
                end try
                try
                    click button "Got it" of w
                end try
                -- Also check for buttons inside groups/sheets
                try
                    repeat with g in (every group of w)
                        try
                            click button "OK" of g
                        end try
                        try
                            click button "Got it" of g
                        end try
                    end repeat
                end try
                -- Check sheets
                try
                    repeat with s in (every sheet of w)
                        try
                            click button "OK" of s
                        end try
                        try
                            click button "Got it" of s
                        end try
                    end repeat
                end try
            end repeat
        end tell
    end tell
    '''
    try:
        run_applescript(script)
    except Exception:
        pass


def start_popup_dismisser():
    """Background thread that keeps trying to dismiss Zoom popups for 2 minutes."""
    def _dismiss_loop():
        for _ in range(40):  # Check every 3 seconds for 2 minutes
            dismiss_zoom_popups()
            time.sleep(3)
    t = threading.Thread(target=_dismiss_loop, daemon=True)
    t.start()


def ensure_obs_running():
    """Launch OBS if not already running."""
    result = subprocess.run(
        ["pgrep", "-x", "obs"], capture_output=True, text=True
    )
    if result.returncode != 0:
        subprocess.run(["open", "-a", "OBS"])
        time.sleep(5)  # Wait for OBS to fully launch


def connect_obs():
    """Connect to OBS WebSocket."""
    import obsws_python as obs
    cl = obs.ReqClient(host="localhost", port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
    return cl


def setup_obs_zoom_capture(obs_client):
    """Auto-create a scene with display capture for recording Zoom.

    If the scene already exists, reuse it without recreating sources.
    Only sets up the scene/sources on first run.
    """
    scene_name = "Zoom Recording"

    # Do NOT change OBS resolution — respect whatever the user has set up manually

    # Check if scene already exists
    scenes = obs_client.get_scene_list()
    scene_names = [s["sceneName"] for s in scenes.scenes]

    if scene_name in scene_names:
        # Scene exists — just switch to it, don't recreate anything
        obs_client.set_current_program_scene(scene_name)
        logger.info("Reusing existing Zoom Recording scene")
        return

    # Create new scene
    obs_client.create_scene(scene_name)
    obs_client.set_current_program_scene(scene_name)
    logger.info(f"Created OBS scene: {scene_name}")

    # Add display capture source
    obs_client.create_input(
        sceneName=scene_name,
        inputName="Zoom Display Capture",
        inputKind="display_capture",
        inputSettings={
            "show_cursor": False,
        },
        sceneItemEnabled=True,
    )
    logger.info("Created OBS display capture source")
    time.sleep(1)

    # Add desktop audio capture so the class audio is recorded
    try:
        obs_client.create_input(
            sceneName=scene_name,
            inputName="Zoom Audio",
            inputKind="coreaudio_output_capture",
            inputSettings={},
            sceneItemEnabled=True,
        )
        logger.info("Created OBS audio source")
    except Exception as e:
        logger.warning(f"Could not create audio source: {e}")

    logger.info("OBS scene setup complete")


def start_obs_recording(obs_client):
    """Start recording in OBS."""
    obs_client.start_record()


def stop_obs_recording(obs_client):
    """Stop recording in OBS."""
    result = obs_client.stop_record()
    return result.output_path


def is_zoom_meeting_active() -> bool:
    """Check if a Zoom meeting is active using multiple signals.

    Uses ps aux instead of pgrep for broader process visibility,
    plus window count as a secondary signal.
    """
    # Method 1: Use ps aux to find meeting-specific processes
    # This is more reliable than pgrep in some environments
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    ps_output = result.stdout.lower()
    has_aomhost = "aomhost" in ps_output
    has_cpthost = "cpthost" in ps_output and "cpthost.app" in ps_output

    if has_aomhost or has_cpthost:
        logger.info(f"Meeting detected via ps: aomhost={has_aomhost}, cpthost={has_cpthost}")
        return True

    # Method 2: Check Zoom window count (idle ~26, meeting ~33)
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        zoom_window_count = sum(1 for w in windows if w.get("kCGWindowOwnerName", "") == "zoom.us")
        if zoom_window_count > 29:
            logger.info(f"Meeting detected via window count: {zoom_window_count}")
            return True
    except Exception:
        pass

    return False


# Store the event loop reference for use in background threads
_event_loop = None


def monitor_zoom_and_stop_recording(bot_token: str, chat_id: int):
    """Background thread: poll Zoom status, stop OBS when meeting ends.

    Strategy:
    1. Wait for meeting to become active (CptHost/aomhost spawned)
    2. Once active, monitor until those processes disappear
    3. If meeting never detected via processes, fall back to monitoring
       whether the Zoom app itself is still running
    """
    logger.info("Started monitoring Zoom meeting...")

    # Single unified loop: detect meeting start, then detect meeting end
    # No initial sleep — start checking immediately every 3 seconds
    meeting_ever_active = False
    consecutive_inactive = 0

    while active_session["recording"]:
        active = is_zoom_meeting_active()

        if active:
            if not meeting_ever_active:
                meeting_ever_active = True
                logger.info("Meeting detected as active")
            consecutive_inactive = 0
        else:
            if meeting_ever_active:
                # Meeting was active before but now it's not
                consecutive_inactive += 1
                logger.info(f"Meeting appears inactive ({consecutive_inactive}/4)")
                if consecutive_inactive >= 4:
                    logger.info("Zoom meeting ended, stopping recording...")
                    try:
                        obs_client = connect_obs()
                        output_path = stop_obs_recording(obs_client)
                        active_session["recording"] = False
                        _send_telegram_sync(bot_token, chat_id, f"Meeting ended. Recording stopped and saved.\n\nFile: {output_path}")
                    except Exception as e:
                        logger.error(f"Error stopping recording: {e}")
                        active_session["recording"] = False
                        _send_telegram_sync(bot_token, chat_id, f"Meeting ended but error stopping recording: {e}")
                    return

        time.sleep(3)


def _send_telegram_sync(bot_token: str, chat_id: int, text: str):
    """Send a Telegram message from a background thread using requests. Retries up to 3 times."""
    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    for attempt in range(3):
        try:
            urllib.request.urlopen(url, data, timeout=15)
            logger.info(f"Telegram notification sent to {chat_id}")
            return
        except Exception as e:
            logger.error(f"Telegram send attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(3)


def run_scheduled_session(meeting_id: str, password: str, class_name: str, chat_id: int):
    """Run a scheduled recording session (called from scheduler thread)."""
    if active_session["recording"]:
        _send_telegram_sync(BOT_TOKEN, chat_id, f"Cannot start scheduled class '{class_name}' — another recording is active.")
        return

    _send_telegram_sync(BOT_TOKEN, chat_id, f"Starting scheduled class: {class_name}\nMeeting ID: {meeting_id}")

    try:
        ensure_obs_running()
        time.sleep(2)

        open_zoom_meeting(meeting_id, password)
        start_popup_dismisser()  # Auto-click "Got it" on recording consent popup
        time.sleep(10)

        obs_client = connect_obs()
        setup_obs_zoom_capture(obs_client)
        time.sleep(2)

        start_obs_recording(obs_client)
        active_session["recording"] = True

        _send_telegram_sync(BOT_TOKEN, chat_id, f"Recording started for '{class_name}'! I'll notify you when it ends.")

        monitor_thread = threading.Thread(
            target=monitor_zoom_and_stop_recording,
            args=(BOT_TOKEN, chat_id),
            daemon=True,
        )
        active_session["monitor_thread"] = monitor_thread
        monitor_thread.start()

    except Exception as e:
        logger.error(f"Error starting scheduled session: {e}")
        active_session["recording"] = False
        _send_telegram_sync(BOT_TOKEN, chat_id, f"Error starting scheduled class '{class_name}': {e}")


def scheduler_loop(chat_id: int):
    """Background thread that checks schedule every 30 seconds and starts sessions."""
    logger.info("Scheduler started")
    while True:
        try:
            schedule = load_schedule()
            now = datetime.now()
            current_date = now.strftime("%Y-%m-%d")
            current_time = now.strftime("%H:%M")
            changed = False

            for entry in schedule:
                schedule_date = entry.get("date", "")
                schedule_time = entry.get("time", "")

                if schedule_date == current_date and schedule_time == current_time:
                    # Check if already started (prevent double-start)
                    if entry.get("done"):
                        continue

                    # Mark as done
                    entry["done"] = True
                    changed = True

                    logger.info(f"Scheduler triggering class: {entry.get('name', 'Unknown')}")
                    session_thread = threading.Thread(
                        target=run_scheduled_session,
                        args=(entry["meeting_id"], entry.get("password", ""), entry.get("name", "Class"), chat_id),
                        daemon=True,
                    )
                    session_thread.start()
                    break  # Only start one class at a time

            # Clean up past classes that are done
            schedule = [e for e in schedule if not (e.get("done") and e.get("date", "") < current_date)]
            changed = True

            if changed:
                save_schedule(schedule)

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(30)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Zoom Class Recorder Bot\n\n"
        "Commands:\n"
        "/join <id> <password> - Join and record now\n"
        "/stop - Stop recording\n"
        "/status - Check status\n"
        "/schedule - View scheduled classes\n"
        "/add <YYYY-MM-DD> <HH:MM> <name> <zoom_link> - Add a class\n"
        "/remove <number> - Remove a scheduled class\n\n"
        "Or just send a Zoom link to join immediately."
    )


async def start_join_session(update: Update, meeting_id: str, password: str, context: ContextTypes.DEFAULT_TYPE):
    """Core logic: join Zoom + setup OBS + start recording."""
    if active_session["recording"]:
        await update.message.reply_text("A recording session is already active. Use /stop to end it first.")
        return

    await update.message.reply_text(f"Starting session...\nMeeting ID: {meeting_id}\nPassword: {'*' * len(password) if password else '(none)'}")

    try:
        # Step 1: Ensure OBS is running
        await update.message.reply_text("1/4 Opening OBS...")
        ensure_obs_running()
        time.sleep(2)

        # Step 2: Join Zoom meeting
        await update.message.reply_text("2/4 Joining Zoom meeting...")
        open_zoom_meeting(meeting_id, password)
        start_popup_dismisser()  # Auto-click "Got it" on recording consent popup
        time.sleep(10)  # Wait for Zoom to fully join

        # Step 3: Setup OBS scene to capture Zoom
        await update.message.reply_text("3/4 Setting up Zoom capture in OBS...")
        obs_client = connect_obs()
        setup_obs_zoom_capture(obs_client)
        time.sleep(2)

        # Step 4: Start OBS recording
        await update.message.reply_text("4/4 Starting OBS recording...")
        start_obs_recording(obs_client)
        active_session["recording"] = True

        await update.message.reply_text("Recording started! I'll notify you when the meeting ends.")

        # Start background monitoring
        monitor_thread = threading.Thread(
            target=monitor_zoom_and_stop_recording,
            args=(BOT_TOKEN, update.effective_chat.id),
            daemon=True,
        )
        active_session["monitor_thread"] = monitor_thread
        monitor_thread.start()

    except Exception as e:
        logger.error(f"Error during session setup: {e}")
        await update.message.reply_text(f"Error: {e}")
        active_session["recording"] = False


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text
    meeting_id, password = parse_zoom_info(text)

    if not meeting_id:
        await update.message.reply_text(
            "Could not parse meeting info.\n\n"
            "Send in one of these formats:\n"
            "/join 123456789 password\n"
            "https://zoom.us/j/123456789?pwd=xxx\n"
            "Meeting ID: 123 456 789 Passcode: xxx"
        )
        return

    await start_join_session(update, meeting_id, password, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not active_session["recording"]:
        await update.message.reply_text("No active recording session.")
        return

    try:
        obs_client = connect_obs()
        output_path = stop_obs_recording(obs_client)
        active_session["recording"] = False
        await update.message.reply_text(f"Recording stopped.\n\nFile: {output_path}")
    except Exception as e:
        active_session["recording"] = False
        await update.message.reply_text(f"Error stopping recording: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    zoom_active = is_zoom_meeting_active()
    status_text = (
        f"Recording: {'Active' if active_session['recording'] else 'Inactive'}\n"
        f"Zoom meeting: {'Active' if zoom_active else 'Not detected'}"
    )
    await update.message.reply_text(status_text)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all scheduled classes."""
    if not is_authorized(update):
        return

    schedule = load_schedule()
    if not schedule:
        await update.message.reply_text("No classes scheduled.\n\nUse /add to add one:\n/add 2026-04-01 09:00 Math https://zoom.us/j/123?pwd=xxx")
        return

    lines = ["Scheduled classes:\n"]
    for i, entry in enumerate(schedule, 1):
        name = entry.get("name", "Unknown")
        date = entry.get("date", "?")
        t = entry.get("time", "?")
        mid = entry.get("meeting_id", "?")
        done = " (done)" if entry.get("done") else ""
        try:
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except ValueError:
            day_name = "?"
        lines.append(f"{i}. {name} — {date} ({day_name}) {t}{done}\n   ID: {mid}")

    await update.message.reply_text("\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a scheduled class.

    Format: /add <YYYY-MM-DD> <HH:MM> <name> <zoom_link_or_id> [password]
    Example: /add 2026-04-01 09:00 Math https://zoom.us/j/123456789?pwd=xxx
    Example: /add 2026-04-03 14:30 Physics 123456789 mypassword
    """
    if not is_authorized(update):
        return

    text = update.message.text
    args_text = text[4:].strip()

    # Parse: date time name zoom_info [password]
    parts = args_text.split(None, 3)
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /add <YYYY-MM-DD> <HH:MM> <name> <zoom_link>\n\n"
            "Examples:\n"
            "/add 2026-04-01 09:00 Math https://zoom.us/j/123?pwd=xxx\n"
            "/add 2026-04-03 14:30 Physics 123456789 mypassword"
        )
        return

    date_str = parts[0]
    try:
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
        if parsed_date.date() < datetime.now().date():
            await update.message.reply_text("That date is in the past.")
            return
    except ValueError:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD (e.g., 2026-04-01)")
        return

    time_str = parts[1]
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        await update.message.reply_text("Invalid time format. Use HH:MM (e.g., 09:00, 14:30)")
        return

    name = parts[2]
    zoom_text = parts[3]

    meeting_id, password = parse_zoom_info(zoom_text)
    if not meeting_id:
        zoom_parts = zoom_text.split()
        if zoom_parts and zoom_parts[0].replace(" ", "").isdigit():
            meeting_id = zoom_parts[0].replace(" ", "")
            password = zoom_parts[1] if len(zoom_parts) > 1 else ""
        else:
            await update.message.reply_text("Could not parse Zoom meeting info. Include a Zoom link or meeting ID.")
            return

    schedule = load_schedule()
    day_name = parsed_date.strftime("%A")
    entry = {
        "name": name,
        "date": date_str,
        "time": time_str,
        "meeting_id": meeting_id,
        "password": password or "",
    }
    schedule.append(entry)
    # Sort by date and time
    schedule.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    save_schedule(schedule)

    await update.message.reply_text(
        f"Class scheduled!\n\n"
        f"Name: {name}\n"
        f"Date: {date_str} ({day_name})\n"
        f"Time: {time_str}\n"
        f"Meeting ID: {meeting_id}\n"
        f"Password: {'set' if password else 'none'}"
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a scheduled class by number."""
    if not is_authorized(update):
        return

    text = update.message.text
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /remove <number>\n\nUse /schedule to see the list.")
        return

    index = int(parts[1]) - 1
    schedule = load_schedule()

    if index < 0 or index >= len(schedule):
        await update.message.reply_text(f"Invalid number. You have {len(schedule)} scheduled classes.")
        return

    removed = schedule.pop(index)
    save_schedule(schedule)
    await update.message.reply_text(f"Removed: {removed.get('name', 'Unknown')} — {removed.get('day', '?').capitalize()} {removed.get('time', '?')}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages that might contain Zoom links or meeting info."""
    if not is_authorized(update):
        return

    text = update.message.text
    meeting_id, password = parse_zoom_info(text)

    if meeting_id:
        # Call the join logic directly
        await start_join_session(update, meeting_id, password, context)
    else:
        await update.message.reply_text(
            "I couldn't find meeting info in your message.\n"
            "Send a Zoom link, or use: /join <id> <password>"
        )


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return

    if not ALLOWED_USER_IDS:
        print("Error: TELEGRAM_USER_ID not set in .env")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start the scheduler in a background thread
    scheduler_thread = threading.Thread(
        target=scheduler_loop,
        args=(ALLOWED_USER_IDS[0],),
        daemon=True,
    )
    scheduler_thread.start()

    logger.info("Bot started. Waiting for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
