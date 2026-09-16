"""
Institutional Trading Coach — Telegram Bot (With SQLite Journaling)
===================================================================
Features:
  - Per-user trade journaling & persistent database storage (SQLite)
  - Commands: /journal, /stats, /admin_stats
  - Gemini 3.6 Flash Integration for SMC/ICT execution coaching
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Configuration & Environment
# --------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")  # Optional: Your Telegram ID for /admin_stats

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    sys.exit("Missing required environment variables.")

SYSTEM_INSTRUCTION = (
    "You are an expert Institutional Trading Coach specializing in SMC "
    "(Smart Money Concepts), ICT, and Gold (XAUUSD). Your job is to act as "
    "a strict, professional personal coach. Help traders pre-check their "
    "risk management, analyze their trade journals, prevent emotional "
    "revenge trading, and enforce discipline. Keep answers sharp, practical, and direct."
)

TELEGRAM_MESSAGE_LIMIT = 4096
TYPING_REFRESH_SECONDS = 4

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("trading_coach_bot")

# --------------------------------------------------------------------------
# Database Setup (SQLite)
# --------------------------------------------------------------------------

DB_PATH = "trading_journal.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            pair TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            risk_pct REAL NOT NULL,
            rr_ratio REAL NOT NULL,
            outcome TEXT NOT NULL, -- WIN, LOSS, BE
            notes TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --------------------------------------------------------------------------
# Gemini Client & Per-Chat Sessions
# --------------------------------------------------------------------------

genai_client = genai.Client(api_key=GEMINI_API_KEY)
_chat_sessions: Dict[int, Any] = {}

def get_chat_session(chat_id: int) -> Any:
    session = _chat_sessions.get(chat_id)
    if session is None:
        session = genai_client.aio.chats.create(
            model=GEMINI_MODEL,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.4,
            ),
        )
        _chat_sessions[chat_id] = session
    return session

def reset_chat_session(chat_id: int) -> None:
    _chat_sessions.pop(chat_id, None)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT):
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        yield remaining[:split_at]
        remaining = remaining[split_at:].lstrip()

async def send_long_message(update: Update, text: str) -> None:
    for chunk in split_message(text):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.message.reply_text(chunk)

async def _keep_typing(bot, chat_id: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            pass

# --------------------------------------------------------------------------
# Commands (Journal, Stats, Admin)
# --------------------------------------------------------------------------

WELCOME_TEXT = (
    "*Institutional Trading Coach* — Online.\n\n"
    "Commands available:\n"
    "• `/log Pair | Setup | Risk% | RR | Outcome | Notes` — Log a trade\n"
    "  *(Example: `/log XAUUSD | FVG Sweep | 1.0 | 2.5 | WIN | Swept Asian high`)*\n"
    "• `/stats` — View your personal performance & metrics\n"
    "• `/journal` — View your last 5 logged trades\n"
    "• `/reset` — Clear active conversation memory\n"
    "• `/help` — Show this message"
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN)

async def log_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split("|")]

    if len(parts) < 5:
        await update.message.reply_text(
            "⚠️ *Invalid Format!*\nUse: `/log Pair | Setup | Risk% | RR | WIN/LOSS/BE | Notes`\n"
            "Example:\n`/log XAUUSD | FVG Sweep | 1.0 | 3.0 | WIN | Swept liquidity`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pair, setup, risk_pct, rr, outcome = parts[0], parts[1], parts[2], parts[3], parts[4].upper()
    notes = parts[5] if len(parts) > 5 else "None"

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (user_id, username, pair, setup_type, risk_pct, rr_ratio, outcome, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user.id, user.username or user.first_name, pair, setup, float(risk_pct), float(rr), outcome, notes))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ *Trade Logged Successfully!*\nPair: `{pair}` | Outcome: `{outcome}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("DB Error: %s", e)
        await update.message.reply_text("⚠️ Failed to log trade. Ensure Risk and RR are numbers.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*), outcome FROM trades WHERE user_id = ? GROUP BY outcome", (user_id,))
    results = cursor.fetchall()
    
    cursor.execute("SELECT AVG(rr_ratio), AVG(risk_pct) FROM trades WHERE user_id = ?", (user_id,))
    avg_rr, avg_risk = cursor.fetchone()
    conn.close()

    if not results:
        await update.message.reply_text("No trades logged yet. Use `/log` to add your first trade!")
        return

    stats = {outcome: count for count, outcome in results}
    wins = stats.get("WIN", 0)
    losses = stats.get("LOSS", 0)
    total = sum(stats.values())
    win_rate = (wins / total * 100) if total > 0 else 0

    msg = (
        f"📊 *Your Execution Metrics*\n\n"
        f"• *Total Trades Logged:* {total}\n"
        f"• *Win Rate:* {win_rate:.1f}%\n"
        f"• *Wins:* {wins} | *Losses:* {losses} | *BE:* {stats.get('BE', 0)}\n"
        f"• *Avg Risk/Trade:* {avg_risk or 0:.2f}%\n"
        f"• *Avg RR Ratio:* {avg_rr or 0:.2f}R"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        await update.message.reply_text("Unauthorized.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT username, COUNT(*), 
               SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END)
        FROM trades GROUP BY user_id
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No user data recorded yet.")
        return

    report = "👑 *Admin Overview (All Users)*\n\n"
    for username, total, wins in rows:
        wr = (wins / total * 100) if total > 0 else 0
        report += f"• *@{username}*: {total} trades | Win Rate: {wr:.1f}%\n"

    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

# --------------------------------------------------------------------------
# Message Handlers & Core Loop
# --------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))

    try:
        session = get_chat_session(chat_id)
        response = await session.send_message(user_text)
        reply_text = (response.text or "").strip()
    except genai_errors.APIError as exc:
        logger.error("Gemini API error: %s", exc)
        reply_text = "⚠️ AI Backend error. Try again in a moment."
    finally:
        stop_typing.set()
        await typing_task

    await send_long_message(update, reply_text)

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("log", log_trade_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting with database support...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()