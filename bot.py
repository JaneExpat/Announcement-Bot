"""
Announcement Bot - Telegram bot for broadcasting announcements to subscribers,
groups, and channels. Uses only the Telegram Bot API (no external services).

Features:
- /subscribe, /unsubscribe   - users opt in/out of DMs from the bot
- Auto-tracks groups/channels the bot is added to (as long as it has permission
  to post there)
- /broadcast (admin-only)    - guided flow to compose and send an announcement:
    1. Send the content (text, photo, video, or document w/ caption)
    2. Optionally add inline buttons ("Button Text - https://url.com" per line)
    3. Choose targets: Subscribers / Groups / Channels / any combination
    4. Send now, or schedule for a later date/time
- /stats (admin-only)        - subscriber/group/channel counts + broadcasts sent
- /cancel                    - cancel an in-progress /broadcast flow

Setup:
1. pip install python-telegram-bot[job-queue] --upgrade
2. Create a bot via @BotFather, get your token
3. Set BOT_TOKEN and ADMIN_IDS as environment variables (see below)
4. Add the bot to your groups/channels as ADMIN (needs "Post Messages" rights
   in channels, and should be an admin in groups for reliability)
5. Run: python announcement_bot.py

Environment variables:
- BOT_TOKEN   (required) - your bot token from BotFather
- ADMIN_IDS   (required) - comma-separated Telegram user IDs allowed to run
                            /broadcast and /stats, e.g. "111111,222222"
- DB_PATH     (optional) - path to the SQLite file, defaults to "announce.db"

Scheduling notes:
- Scheduled announcements are stored in SQLite, so they survive bot restarts.
- On startup, any announcement whose scheduled time already passed while the
  bot was offline is sent immediately (catch-up); future ones are re-armed.
- Schedule times are interpreted as UTC. Enter them as "YYYY-MM-DD HH:MM".
"""

import io
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.environ.get("DB_PATH", "announce.db")

# Conversation states
CONTENT, BUTTONS, TARGETS, SCHEDULE = range(4)


# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                title TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                buttons TEXT,
                targets TEXT NOT NULL,
                send_at TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS broadcast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                targets TEXT,
                recipients INTEGER
            )"""
        )
        conn.commit()


def db_execute(query: str, params: tuple =
