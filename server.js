require('dotenv').config();
const express = require('express');
const cors = require('cors');
const { TelegramClient, Api } = require('telegram');
const { StringSession } = require('telegram/sessions');

const app = express();
app.use(cors());
app.use(express.json());

// ទាញយក Keys ពី .env
const apiId = Number(process.env.TELEGRAM_API_ID);
const apiHash = process.env.TELEGRAM_API_HASH;

// Session សម្រាប់រក្សាទុកស្ថានភាព Login បណ្តោះអាសន្ន
const sessions = {};

// 1. API សម្រាប់ផ្ញើ OTP ទៅ Telegram App
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

        // រក្សាទុក client និង hash តាមលេខទូរស័ព្ទ
        sessions[phoneNumber] = { client, phoneCodeHash };

        res.json({ success: true, message: "OTP code sent to your Telegram app!" });
    } catch (err) {
        console.error("Error sending code:", err);
        res.status(500).json({ success: false, error: err.message || "Failed to send code" });
    }
});

// 2. API សម្រាប់ផ្ទៀងផ្ទាត់ OTP
app.post('/api/verify-code', async (req, res) => {
    const { phoneNumber, code } = req.body;

    if (!phoneNumber || !code) {
        return res.status(400).json({ success: false, error: "Phone number and code required" });
    }

    const session = sessions[phoneNumber];
    if (!session) {
        return res.status(400).json({ success: false, error: "Session expired. Request code again." });
    }

    try {
        const { client, phoneCodeHash } = session;

        await client.invoke(
            new Api.auth.SignIn({
                phoneNumber: phoneNumber,
                phoneCodeHash: phoneCodeHash,
                phoneCode: code,
            })
        );

        const sessionString = client.session.save();
        delete sessions[phoneNumber]; // សម្អាត Session បន្ទាប់ពី Login ជោគជ័យ

        res.json({
            success: true,
            message: "Authentication successful!",
            session: sessionString
        });
    } catch (err) {
        console.error("Error verifying code:", err);
        res.status(400).json({ success: false, error: err.message || "Verification failed" });
    }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`🚀 Server is running on http://localhost:${PORT}`);
});
