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

# Control bot token
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

def remove_connection(owner_id, source_id):
    with db_connect() as db:
        cursor = db.execute("""
            DELETE FROM connections
            WHERE owner_id = ? AND source_id = ?
        """, (owner_id, source_id))
        return cursor.rowcount > 0

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
        [Button.inline("🚀 START MULTI-CHANNEL SPEED", b"start_transfer")],
        [Button.inline("⏹ STOP TRANSFER", b"stop_transfer")],
    ]

# ==========================================
# VIDEO TRANSFER FUNCTION (FAST, CLEAN, PROTECTED)
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

    file_path = None
    try:
        logging.info("Downloading video concurrently: source=%s message=%s", source_id, message.id)
        file_path = await message.download_media()

        if not file_path:
            logging.error("Failed to download media for message=%s", message.id)
            return

        # Clean upload (caption=None)
        await user_client.send_file(
            target_id,
            file_path,
            caption=None,
            supports_streaming=True
        )

        mark_processed(source_id, message.id, target_id)

        logging.info(
            "⚡ Transferred at full speed: source=%s message=%s target=%s",
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

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

# ==========================================
# PROCESS ALL CHANNELS CONCURRENTLY (ULTRA FAST)
# ==========================================

async def process_single_connection(owner_id, source_id, target_id, sem):
    logging.info("Starting multi-channel fetch from source=%s to target=%s", source_id, target_id)
    try:
        tasks = []
        async for message in user_client.iter_messages(source_id, reverse=True):
            if not transfer_status(owner_id):
                break
            
            if not (message.video or (
                message.document
                and message.document.mime_type
                and message.document.mime_type.startswith("video/")
            )):
                continue

            if already_processed(source_id, message.id, target_id):
                continue

            async def bounded_transfer(msg=message):
                async with sem:
                    if transfer_status(owner_id):
                        await transfer_video(msg, source_id, target_id)

            tasks.append(bounded_transfer())

            if len(tasks) >= 6:
                await asyncio.gather(*tasks)
                tasks = []

        if tasks:
            await asyncio.gather(*tasks)

    except Exception as e:
        logging.error("Error in channel fetch for source %s: %s", source_id, e)

async def process_old_messages(owner_id):
    connections = get_connections(owner_id)
    if not connections:
        return
    
    # Global concurrency semaphore across ALL channels combined
    sem = asyncio.Semaphore(5)

    # Saare channels/groups ko ek saath (concurrently) run karo!
    await asyncio.gather(*(
        process_single_connection(owner_id, source_id, target_id, sem)
        for source_id, target_id in connections
    ))

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
        "⚡ MULTI-CHANNEL FULL-SPEED TRANSFER BOT\n\n"
        "Commands:\n"
        "• /remove <source_id> - Remove source & target connection\n\n"
        "Press START MULTI-CHANNEL SPEED to launch all transfers simultaneously.",
        buttons=main_menu()
    )

@bot_client.on(events.NewMessage(pattern=r"^/remove"))
async def remove_handler(event):
    owner_id = event.sender_id
    if owner_id not in OWNER_IDS:
        await event.respond("⛔ You are not authorised to use this bot.")
        return

    parts = event.raw_text.strip().split()
    if len(parts) < 2:
        await event.respond("❌ Please provide the source channel ID.\nUsage: `/remove -1004370545369`")
        return

    source_id = valid_channel_id(parts[1])
    if source_id is None:
        await event.respond("❌ Invalid source channel ID format.")
        return

    removed = remove_connection(owner_id, source_id)
    if removed:
        await event.respond(f"✅ Connection for source `{source_id}` has been completely removed! Videos will stop transferring.")
    else:
        await event.respond(f"❌ No active connection found for source `{source_id}`.")

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
            "🚀 MULTI-CHANNEL SPEED ACTIVATED!\n\n"
            "All added groups and channels are now transferring videos simultaneously at max speed."
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

            asyncio.create_task(transfer_video(event.message, configured_source, target_id))

# ==========================================
# MAIN
# ==========================================

async def main():
    init_db()

    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)

    logging.info("Multi-Channel Full-Speed Video Transfer Bot started.")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
