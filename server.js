require('dotenv').config();
const express = require('express');
const cors = require('cors');
const path = require('path');
const { TelegramClient, Api } = require('telegram');
const { StringSession } = require('telegram/sessions');

const app = express();
app.use(cors());
app.use(express.json());

// Serve static files (index.html)
app.use(express.static(__dirname));

// ទាញយក Environment Variables ពី Railway Settings
const apiId = Number(process.env.TELEGRAM_API_ID);
const apiHash = process.env.TELEGRAM_API_HASH;

// Session បណ្តោះអាសន្នសម្រាប់រក្សាទុក Telegram Client
const sessions = {};

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

        // រក្សាទុក Client តាមលេខទូរស័ព្ទ
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

        // ប្រសិនបើមានផ្ញើ 2FA Password មក
        if (password) {
            await client.checkPassword({ password: password });
        } else {
            // SignIn ជាមួយ OTP ធម្មតា
            await client.invoke(
                new Api.auth.SignIn({
                    phoneNumber: phoneNumber,
                    phoneCodeHash: phoneCodeHash,
                    phoneCode: code,
                })
            );
        }

        const sessionString = client.session.save();
        delete sessions[phoneNumber]; // សម្អាត Session បន្ទាប់ពី Sign In រួច

        res.json({
            success: true,
            message: "Authentication successful!",
            session: sessionString
        });
    } catch (err) {
        console.error("Error verifying code:", err);

        // ត្រួតពិនិត្យករណី Telegram ទាមទារ 2FA Password
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
