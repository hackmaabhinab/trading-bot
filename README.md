# Institutional Trading Coach — Telegram Bot

A Telegram bot with a strict SMC / ICT / Gold (XAUUSD) trading-discipline
coach persona, powered by Gemini 2.5 Flash.

## ⚠️ About the credentials you shared in chat

The bot token and Gemini API key you pasted were sent in plain text in this
conversation, so you should treat them as compromised and **rotate both**
before going live:

- **Telegram**: open `@BotFather` → `/mybots` → select your bot → *API
  Token* → *Revoke current token*. This invalidates the old one and issues
  a fresh one.
- **Gemini**: go to [Google AI Studio](https://aistudio.google.com/apikey),
  delete the exposed key, and generate a new one.

This script reads both values from environment variables instead of
hardcoding them in source — that's what actually makes credential handling
"production-ready" (a hardcoded secret gets committed to git, pasted into a
support ticket, or leaked the moment the file is shared).

## 1. Install dependencies

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
```

## 2. Configure credentials

```bash
# Windows:
copy .env.example .env
# macOS / Linux:
cp .env.example .env
```

Open `.env` and paste in your **new, rotated** Telegram token and Gemini
API key.

## 3. Run the bot

```bash
python bot.py
```

You should see `Institutional Trading Coach bot starting (polling)...` in
the terminal. Message your bot on Telegram to test it.

## 4. Clearing background instances (avoiding `telegram.error.Conflict`)

Telegram only allows **one** active polling connection per bot token. If
you start a second instance (e.g. you forgot a terminal was still running,
or an IDE auto-restarted it), you'll get `telegram.error.Conflict:
terminated by other getUpdates request`. Before starting the bot, make
sure nothing else is already running:

**Windows (PowerShell or cmd):**
```powershell
tasklist | findstr python
taskkill /F /PID <pid_from_above>
REM or, to kill every python.exe process:
taskkill /F /IM python.exe
```

**macOS / Linux:**
```bash
ps aux | grep bot.py
kill -9 <pid_from_above>
# or, to match by script name directly:
pkill -f bot.py
```

Then start it fresh with `python bot.py`. The script also calls
`run_polling(drop_pending_updates=True)`, which discards any backlog of
missed messages on startup — this avoids most stale-connection Conflict
errors even if the previous process wasn't cleanly stopped.

## What's implemented

- **Persona** — the exact system instruction you specified, sent as
  `system_instruction` on every Gemini call.
- **Per-chat memory** — each Telegram chat gets its own Gemini chat
  session, so the coach remembers the conversation (risk rules already
  discussed, the journal entry you shared, etc.) within that chat.
  In-memory only; resets on restart. Swap in a database if you need it to
  survive restarts.
- **Typing indicator** — refreshed every few seconds for the whole
  duration of the Gemini call (a single `send_chat_action` only lasts
  ~5 seconds, and coaching answers can take longer).
- **Error handling** — Gemini API errors and unexpected exceptions are
  caught per-message (the user gets a clean error reply, not a crash), plus
  a global `Application` error handler that logs anything else.
- **Long replies** — Gemini responses over Telegram's 4096-character limit
  are split on paragraph/line boundaries instead of getting rejected.
- **Markdown with fallback** — tries to send Gemini's Markdown-flavored
  output as formatted text; falls back to plain text if a chunk isn't
  valid Telegram Markdown.
- **Commands** — `/start`, `/help`, `/reset` (clears that chat's memory).

## Natural extensions (not built — flag if you want these added)

- Persist chat history in SQLite/Postgres so context survives restarts.
- Gate features by your Operator membership tiers (Level 1/2/3).
- Session/kill-zone alerts (London/NY) via `telegram.ext.JobQueue`.
- Structured trade-journal logging (store entries, not just chat).
