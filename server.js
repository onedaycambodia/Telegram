// ២. API ផ្ទៀងផ្ទាត់ OTP ( support 2FA Password)
app.post('/api/verify-code', async (req, res) => {
    const { phoneNumber, code, password } = req.body;

    if (!phoneNumber || !code) {
        return res.status(400).json({ success: false, error: "Phone number and code required" });
    }

    const session = sessions[phoneNumber];
    if (!session) {
        return res.status(400).json({ success: false, error: "Session expired. Request code again." });
    }

    try {
        const { client, phoneCodeHash } = session;

        // ប្រសិនបើមានផ្ញើ Password មកជាមួយ (ករណីមាន 2FA)
        if (password) {
            await client.checkPassword({ password: password });
        } else {
            // ព្យាយាម SignIn ជាមួយ OTP
            await client.invoke(
                new Api.auth.SignIn({
                    phoneNumber: phoneNumber,
                    phoneCodeHash: phoneCodeHash,
                    phoneCode: code,
                })
            );
        }

        const sessionString = client.session.save();
        delete sessions[phoneNumber];

        res.json({
            success: true,
            message: "Authentication successful!",
            session: sessionString
        });
    } catch (err) {
        console.error("Error verifying code:", err);

        // បើ Telegram ទារ 2FA Password
        if (err.message && err.message.includes("SESSION_PASSWORD_NEEDED")) {
            return res.status(401).json({
                success: false,
                requiresPassword: true,
                error: "Two-Step Verification password is required."
            });
        }

        res.status(400).json({ success: false, error: err.message || "Verification failed" });
    }
});
