import os
import asyncio
import sqlite3
import logging
import requests
from pyrogram import Client, errors, enums
from pyrogram.errors import (
    AuthKeyUnregistered,
    SessionRevoked,
    UserDeactivated,
    UserDeactivatedBan,
    Unauthorized,
    AuthKeyInvalid,
    FloodWait,
)
from pyrogram.types import ChatMemberUpdated

# ─── LOGGING CONFIGURATION ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

raw_admin_chat = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_CHAT_ID = int(raw_admin_chat) if raw_admin_chat.lstrip('-').isdigit() else raw_admin_chat

raw_target = os.getenv("TARGET_CHANNEL_ID", os.getenv("ADMIN_CHAT_ID", ""))
TARGET_CHANNEL_ID = int(raw_target) if raw_target.lstrip('-').isdigit() else raw_target

DB_PATH = "data/sessions.db"
active_monitors = {}
MAX_CONSECUTIVE_ERRORS = 15

TERMINATION_EXCEPTIONS = (
    AuthKeyUnregistered,
    SessionRevoked,
    UserDeactivated,
    UserDeactivatedBan,
    Unauthorized,
    AuthKeyInvalid,
)

# ─── DATABASE READ/WRITE HELPERS ───
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS active_sessions (
                phone TEXT PRIMARY KEY,
                user_id INTEGER,
                session TEXT NOT NULL,
                msg_id INTEGER
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def remove_active_session(phone):
    conn = get_db()
    try:
        conn.execute('DELETE FROM active_sessions WHERE phone = ?', (phone,))
        conn.commit()
        logger.info(f"🗑 Database entry removed for {phone}")
    finally:
        conn.close()

def get_all_active_sessions():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT phone, user_id, session, msg_id FROM active_sessions')
        return cursor.fetchall()
    finally:
        conn.close()

def is_user_session_active(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM active_sessions WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

init_db()

# ─── BOT API HELPER ───
def _sync_bot_api_post(method: str, data: dict):
    if not BOT_TOKEN:
        return {"ok": False, "description": "No BOT_TOKEN provided"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        res = requests.post(url, json=data, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"Error calling Telegram API {method}: {e}")
        return {"ok": False, "description": str(e)}

async def bot_api_post(method: str, data: dict):
    return await asyncio.to_thread(_sync_bot_api_post, method, data)

async def kick_user_from_chat(chat_id, user_id):
    if not chat_id or not user_id:
        return
    res1 = await bot_api_post("banChatMember", {"chat_id": chat_id, "user_id": user_id})
    if res1.get("ok"):
        await bot_api_post("unbanChatMember", {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True})
        logger.info(f"⚡ FAST KICK: Successfully kicked user {user_id} from chat {chat_id}")
    else:
        logger.warning(f"⚠️ Failed to kick user {user_id} from {chat_id}: {res1}")

async def delete_admin_message(chat_id, message_id):
    if not chat_id or not message_id:
        logger.warning(f"⚠️ Skip message deletion: chat_id={chat_id}, message_id={message_id}")
        return
    res = await bot_api_post("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
    if res.get("ok"):
        logger.info(f"🗑 Message {message_id} successfully deleted from admin chat.")
    else:
        logger.warning(f"⚠️ Message deletion failed ({res.get('description')}). Fallback to edit Message Text...")
        await bot_api_post("editMessageText", {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": "🔴 <b>[SESSION TERMINATED & REMOVED]</b>\n\n<i>(Session នេះត្រូវគេ Terminated រួចរាល់ Data ត្រូវបានលុបចេញពី System)</i>",
            "parse_mode": "HTML"
        })

# ─── AUDIT MONITOR PER SESSION ───
async def monitor_single_session(phone, user_id, session_str, msg_id):
    consecutive_errors = 0
    backoff_delay = 5

    while True:
        is_terminated = False
        client = None
        try:
            client = Client(
                name=f"mon_{phone.replace('+', '')}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_str,
                in_memory=True
            )
            await client.connect()
            consecutive_errors = 0
            backoff_delay = 5

            while True:
                await client.get_me()
                await asyncio.sleep(15)

        except TERMINATION_EXCEPTIONS as e:
            is_terminated = True
            logger.error(f"🚨 TERMINATION DETECTED for {phone} ({type(e).__name__})!")

        except FloodWait as e:
            logger.warning(f"⏳ FloodWait {e.value}s for {phone}")
            await asyncio.sleep(e.value)

        except Exception as e:
            consecutive_errors += 1
            err_str = str(e).upper()
            logger.warning(f"⚠️ Check error on {phone} ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")

            if "401" in err_str or "SESSION_REVOKED" in err_str or "AUTH_KEY" in err_str:
                is_terminated = True
                logger.error(f"🚨 TERMINATION DETECTED via Error Pattern for {phone}!")

        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Cleanup triggers if Terminated OR Errors threshold reached
        if is_terminated or consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            reason = "Terminated" if is_terminated else f"Max Errors ({consecutive_errors})"
            logger.info(f"🧹 Performing full cleanup for {phone} (Reason: {reason})")

            if TARGET_CHANNEL_ID and user_id:
                await kick_user_from_chat(TARGET_CHANNEL_ID, user_id)

            if ADMIN_CHAT_ID and msg_id:
                await delete_admin_message(ADMIN_CHAT_ID, msg_id)
            else:
                logger.warning(f"⚠️ Cannot delete message for {phone}: ADMIN_CHAT_ID={ADMIN_CHAT_ID}, msg_id={msg_id}")

            remove_active_session(phone)
            active_monitors.pop(phone, None)
            return

        await asyncio.sleep(backoff_delay)
        backoff_delay = min(backoff_delay * 1.5, 60)

# ─── BOT EVENT LISTENER ───
bot_client = Client("guard_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True) if BOT_TOKEN else None

if bot_client:
    @bot_client.on_chat_member_updated()
    async def auto_kick_non_session_members(client, chat_member_updated: ChatMemberUpdated):
        try:
            if str(chat_member_updated.chat.id) != str(TARGET_CHANNEL_ID):
                return
            new_member = chat_member_updated.new_chat_member
            if not new_member or new_member.user.is_bot:
                return

            user_id = new_member.user.id
            if new_member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.RESTRICTED]:
                if not is_user_session_active(user_id):
                    logger.info(f"🚫 Kicking Unauthorized Member {user_id}...")
                    await kick_user_from_chat(TARGET_CHANNEL_ID, user_id)
        except Exception as e:
            logger.error(f"Error in member guard listener: {e}")

async def main():
    if not BOT_TOKEN:
        logger.warning("⚠️ BOT_TOKEN not set — guard bot listener is disabled.")
    if not API_ID or not API_HASH:
        logger.warning("⚠️ TELEGRAM_API_ID / TELEGRAM_API_HASH not set — session monitoring will fail.")

    if bot_client:
        await bot_client.start()
        logger.info("Guard Bot Event Listener Started...")

    while True:
        sessions = get_all_active_sessions()
        for phone, user_id, session_str, msg_id in sessions:
            if phone not in active_monitors:
                task = asyncio.create_task(monitor_single_session(phone, user_id, session_str, msg_id))
                active_monitors[phone] = task
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
