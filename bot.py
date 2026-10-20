import os
import re
import json
import sqlite3
import asyncio
import logging

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
)

# ==========================================
# TELEGRAM API CONFIGURATION
# ==========================================

api_id = 37093326  
api_hash = "6e370e6d265a4866e3af0da44c8334da"  

# Control bot ka token (Direct fixed)
BOT_TOKEN = "8890372929:AAFpTlUJsz4ddPfAWMNYjKVlrwLZhVh_khM"

# User account session: Railway Variables
SESSION_STRING = os.environ.get("SESSION_STRING", "")

if not SESSION_STRING:
    raise RuntimeError("SESSION_STRING Railway Variables mein set karo.")

# ==========================================
# DATABASE
# ==========================================

DB_PATH = "video_transfer.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def db_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                UNIQUE(owner_id, source_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                source_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                PRIMARY KEY(source_id, message_id, target_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                owner_id INTEGER PRIMARY KEY,
                transfer_enabled INTEGER DEFAULT 0
            )
        """)

def get_connections(owner_id):
    with db_connect() as db:
        return db.execute("""
            SELECT source_id, target_id
            FROM connections
            WHERE owner_id = ? AND enabled = 1
        """, (owner_id,)).fetchall()

def save_connection(owner_id, source_id, target_id):
    with db_connect() as db:
        db.execute("""
            INSERT INTO connections(owner_id, source_id, target_id)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_id, source_id)
            DO UPDATE SET target_id = excluded.target_id,
                          enabled = 1
        """, (owner_id, source_id, target_id))

def already_processed(source_id, message_id, target_id):
    with db_connect() as db:
        return db.execute("""
            SELECT 1 FROM processed
            WHERE source_id = ? AND message_id = ? AND target_id = ?
        """, (source_id, message_id, target_id)).fetchone() is not None

def mark_processed(source_id, message_id, target_id):
    with db_connect() as db:
        db.execute("""
            INSERT OR IGNORE INTO processed
            VALUES (?, ?, ?)
        """, (source_id, message_id, target_id))

def transfer_status(owner_id):
    with db_connect() as db:
        row = db.execute("""
            SELECT transfer_enabled FROM settings WHERE owner_id = ?
        """, (owner_id,)).fetchone()
        return bool(row and row[0])

def set_transfer_status(owner_id, enabled):
    with db_connect() as db:
        db.execute("""
            INSERT INTO settings(owner_id, transfer_enabled)
            VALUES (?, ?)
            ON CONFLICT(owner_id)
            DO UPDATE SET transfer_enabled = excluded.transfer_enabled
        """, (owner_id, int(enabled)))

# ==========================================
# TELEGRAM CLIENTS
# ==========================================

user_client = TelegramClient(
    StringSession(SESSION_STRING),
    api_id,
    api_hash
)

bot_client = TelegramClient(
    "control_bot_session",
    api_id,
    api_hash
)

pending = {}

OWNER_IDS = {8963867689}

# ==========================================
# HELPERS
# ==========================================

def valid_channel_id(text):
    text = text.strip()
    if not re.fullmatch(r"-?\d+", text):
        return None
    value = int(text)
    if value > 0:
        return None
    return value

def main_menu():
    return [
        [Button.inline("➕ ADD ANOTHER CHANNEL", b"add")],
        [Button.inline("📋 VIEW CONNECTIONS", b"view")],
        [Button.inline("🚀 START TRANSFER & OLD VIDEOS", b"start_transfer")],
        [Button.inline("⏹ STOP TRANSFER", b"stop_transfer")],
    ]

# ==========================================
# VIDEO TRANSFER FUNCTION (CLEAN VIDEOS)
# ==========================================

async def transfer_video(message, source_id, target_id):
    if not (message.video or (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
    )):
        return

    if already_processed(source_id, message.id, target_id):
        return

    try:
        # caption=None karne se video bilkul clean jayegi (koi text ya link nahi aayega)
        await user_client.send_file(
            target_id,
            message.media,
            caption=None,
            supports_streaming=True
        )

        mark_processed(source_id, message.id, target_id)

        logging.info(
            "Transferred clean video source=%s message=%s target=%s",
            source_id, message.id, target_id
        )

    except FloodWaitError as e:
        logging.warning("Telegram FloodWait: %s seconds", e.seconds)
        await asyncio.sleep(e.seconds)

    except (ChatAdminRequiredError, ChannelPrivateError) as e:
        logging.error("Access/permission error: %s", e)

    except Exception:
        logging.exception(
            "Transfer failed: source=%s message=%s target=%s",
            source_id, message.id, target_id
        )

# ==========================================
# PROCESS OLD MESSAGES (HISTORY)
# ==========================================

async def process_old_messages(owner_id):
    connections = get_connections(owner_id)
    for source_id, target_id in connections:
        logging.info("Fetching old clean videos from source=%s to target=%s", source_id, target_id)
        try:
            async for message in user_client.iter_messages(source_id, reverse=True):
                if not transfer_status(owner_id):
                    break
                await transfer_video(message, source_id, target_id)
        except Exception as e:
            logging.error("Error fetching old messages for source %s: %s", source_id, e)

# ==========================================
# CONTROL BOT
# ==========================================

@bot_client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    if event.sender_id not in OWNER_IDS:
        await event.respond("⛔ You are not authorised to use this bot.")
        return

    pending.pop(event.sender_id, None)

    await event.respond(
        "🎬 CLEAN VIDEO AUTO TRANSFER BOT\n\n"
        "Press START TRANSFER to transfer old clean videos and monitor new ones.",
        buttons=main_menu()
    )

@bot_client.on(events.CallbackQuery)
async def button_handler(event):
    owner_id = event.sender_id

    if owner_id not in OWNER_IDS:
        await event.answer("Not authorised.", alert=True)
        return

    data = event.data

    if data == b"add":
        pending[owner_id] = {"step": "source"}
        await event.edit(
            "📥 Please send your SOURCE CHANNEL ID.\n\n"
            "Your Telegram user account must have access to it."
        )

    elif data == b"view":
        connections = get_connections(owner_id)
        if not connections:
            await event.respond("No connections configured.")
            return

        lines = ["📋 YOUR CONNECTIONS\n"]
        for i, (source, target) in enumerate(connections, 1):
            lines.append(f"{i}. SOURCE: {source}\n   TARGET: {target}\n")
        await event.respond("\n".join(lines))

    elif data == b"start_transfer":
        connections = get_connections(owner_id)
        if not connections:
            await event.answer("Add at least one source and target first.", alert=True)
            return

        set_transfer_status(owner_id, True)
        await event.respond(
            "🚀 CLEAN TRANSFER STARTED!\n\n"
            "Bot is processing old videos without captions and will monitor new ones."
        )
        asyncio.create_task(process_old_messages(owner_id))

    elif data == b"stop_transfer":
        set_transfer_status(owner_id, False)
        await event.respond("⏹ Transfer stopped.")

@bot_client.on(events.NewMessage)
async def setup_handler(event):
    owner_id = event.sender_id
    if owner_id not in OWNER_IDS:
        return

    if event.raw_text and event.raw_text.startswith("/"):
        return

    state = pending.get(owner_id)
    if not state:
        return

    channel_id = valid_channel_id(event.raw_text or "")
    if channel_id is None:
        await event.respond("❌ Invalid ID. Send a numeric Telegram channel/group ID.")
        return

    if state["step"] == "source":
        state["source_id"] = channel_id
        state["step"] = "target"
        await event.respond(f"✅ SOURCE ACCEPTED: {channel_id}\n\nNow send the TARGET CHANNEL/GROUP ID.")

    elif state["step"] == "target":
        source_id = state["source_id"]
        target_id = channel_id
        save_connection(owner_id, source_id, target_id)
        pending.pop(owner_id, None)
        await event.respond(
            f"✅ CONNECTION SAVED\n\nSOURCE: {source_id}\nTARGET: {target_id}\n\nWhat would you like to do next?",
            buttons=main_menu()
        )

# ==========================================
# LIVE MONITOR
# ==========================================

@user_client.on(events.NewMessage)
async def live_handler(event):
    source_id = event.chat_id
    if source_id is None:
        return

    for owner_id in OWNER_IDS:
        if not transfer_status(owner_id):
            continue

        for configured_source, target_id in get_connections(owner_id):
            if source_id != configured_source:
                continue

            await transfer_video(event.message, configured_source, target_id)

# ==========================================
# MAIN
# ==========================================

async def main():
    init_db()

    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)

    logging.info("Clean Video Transfer Bot started.")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
            
