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
        return res.status
