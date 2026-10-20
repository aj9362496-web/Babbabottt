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

api_id = 37093326  # Apni API ID daalo
api_hash = "6e370e6d265a4866e3af0da44c8334da"  # Apna API HASH daalo

# Control bot ka token (Direct fix kar diya hai)
BOT_TOKEN = "8890372929:AAE9-x31PtV43DDwuzRzQ9aPWVoCsNCN46s"

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

# Pending setup steps
pending = {}

# Only IDs listed here can control the bot.
# Set OWNER_IDS in Railway, e.g. "123456789"
OWNER_IDS = {
    int(x.strip())
    for x in os.environ.get("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}

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
        [Button.inline("🚀 START TRANSFER", b"start_transfer")],
        [Button.inline("⏹ STOP TRANSFER", b"stop_transfer")],
    ]

async def send_menu(event, message):
    await event.respond(message, buttons=main_menu())

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
        "🎬 VIDEO AUTO TRANSFER BOT\n\n"
        "Press ADD ANOTHER CHANNEL to create a connection.\n"
        "Each source will have its own target.",
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
        pending[owner_id] = {
            "step": "source"
        }

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
            lines.append(
                f"{i}. SOURCE: {source}\n"
                f"   TARGET: {target}\n"
            )

        await event.respond("\n".join(lines))

    elif data == b"start_transfer":
        connections = get_connections(owner_id)

        if not connections:
            await event.answer(
                "Add at least one source and target first.",
                alert=True
            )
            return

        set_transfer_status(owner_id, True)

        await event.respond(
            "✅ TRANSFER ENABLED\n\n"
            "New videos will be monitored for your configured connections."
        )

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
        await event.respond(
            "❌ Invalid ID. Send a numeric Telegram channel/group ID."
        )
        return

    if state["step"] == "source":
        state["source_id"] = channel_id
        state["step"] = "target"

        await event.respond(
            f"✅ SOURCE ACCEPTED: {channel_id}\n\n"
            "Now send the TARGET CHANNEL/GROUP ID."
        )

    elif state["step"] == "target":
        source_id = state["source_id"]
        target_id = channel_id

        save_connection(owner_id, source_id, target_id)

        pending.pop(owner_id, None)

        await event.respond(
            f"✅ CONNECTION SAVED\n\n"
            f"SOURCE: {source_id}\n"
            f"TARGET: {target_id}\n\n"
            "What would you like to do next?",
            buttons=main_menu()
        )

# ==========================================
# VIDEO TRANSFER
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
        await user_client.send_file(
            target_id,
            message.media,
            caption=message.message or None,
            supports_streaming=True
        )

        mark_processed(source_id, message.id, target_id)

        logging.info(
            "Transferred source=%s message=%s target=%s",
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

            await transfer_video(
                event.message,
                configured_source,
                target_id
            )

# ==========================================
# MAIN
# ==========================================

async def main():
    init_db()

    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)

    logging.info("Video Transfer Bot started.")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
