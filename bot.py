"""
Institutional Trading Coach — Telegram Bot
==========================================
Features:
  - Personal Onboarding (Strategy, Risk & Psychological Profiling)
  - Adaptive Pre-Trade Execution Checklist based on user profile
  - SQLite Trade Journaling & Admin Tracking
  - Powered by Gemini 3.6 Flash
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
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    sys.exit("Missing required environment variables.")

SYSTEM_INSTRUCTION = (
    "You are a strict, world-class Institutional Risk Manager & Trading Coach. "
    "Your objective is to enforce maximum discipline, prevent emotional revenge trading, "
    "and protect capital.\n\n"
    
    "BEHAVIOR RULES:\n"
    "1. NEW USER ONBOARDING: If a user starts a conversation or seems new, ask them concise questions "
    "to extract their trading profile: Their Primary Strategy (e.g., SMC/ICT, Chart Patterns, Price Action), "
    "Max Risk per trade (e.g., 0.5% or 1%), and their biggest Psychological Weakness (e.g., FOMO, Early Exit, Overtrading).\n"
    
    "2. ADAPTIVE PRE-TRADE CHECKLIST: Whenever a user shares a trade setup, idea, or entry signal, DO NOT "
    "give instant approval. Always force a strict, customized 4-Step Pre-Trade Checklist specifically aligned "
    "with THEIR stated strategy and emotional weaknesses. Standard 4 checkpoints to adapt:\n"
    "   - [1. Macro/News Audit]: High-Impact News events cleared?\n"
    "   - [2. Technical Confluence]: Specific entry criteria for THEIR strategy (e.g., HTF Sweep + MSS for SMC, or Break/Retest for PA)?\n"
    "   - [3. Risk Control]: Is risk strictly within their defined limit (<= 1%)?\n"
    "   - [4. Emotional Intent]: Is this a planned session setup or impulsive FOMO/Revenge execution?\n\n"
    
    "3. TONALITY: Direct, firm, professional, and zero fluff. Treat the trader like a funded prop-firm operator."
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
            outcome TEXT NOT NULL,
            notes TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            chat_id INTEGER PRIMARY KEY,
            profile_text TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        profile = get_user_profile(chat_id)
        system_instruction = build_system_instruction_with_profile(profile)
        session = genai_client.aio.chats.create(
            model=GEMINI_MODEL,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
            ),
        )
        _chat_sessions[chat_id] = session
    return session

def reset_chat_session(chat_id: int) -> None:
    _chat_sessions.pop(chat_id, None)

def save_user_profile(chat_id: int, profile_text: str) -> None:
    """Save user's onboarding profile (strategy, risk, weakness) to SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO profiles (chat_id, profile_text, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            profile_text = excluded.profile_text,
            updated_at = CURRENT_TIMESTAMP
    """, (chat_id, profile_text))
    conn.commit()
    conn.close()
    logger.info(f"Profile saved for chat {chat_id}")

def get_user_profile(chat_id: int) -> str | None:
    """Retrieve user's saved profile from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT profile_text FROM profiles WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def build_system_instruction_with_profile(profile_text: str | None) -> str:
    """Inject user profile into system instruction for adaptive coaching."""
    base = SYSTEM_INSTRUCTION
    if profile_text:
        injection = f"\n\n---\n*USER PROFILE (ONBOARDED)*:\n{profile_text}\n---\n"
        return base + injection
    return base

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

async def _keep_typing(bot, chat_id: int, stop_event: asyncio.event) -> None:
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
# Commands (Journal, Stats, Onboarding)
# --------------------------------------------------------------------------

ONBOARDING_TEXT = (
    "*Institutional Risk & Execution Coach* — Online.\n\n"
    "Before we begin, let's establish your trading baseline. Answer these concisely:\n\n"
    "1️⃣ *What is your Primary Strategy?*\n"
    "   (e.g., SMC/ICT, Price Action, Trend Breakouts, FVG Sweeps)\n\n"
    "2️⃣ *What is your Max Risk Per Trade?*\n"
    "   (e.g., 0.5%, 1%, or your chosen percentage)\n\n"
    "3️⃣ *What is your Biggest Execution/Psychology Weakness?*\n"
    "   (e.g., FOMO, Revenge Trading, Overtrading, Early Exits)\n\n"
    "Reply with all 3, separated by `|`. Example:\n"
    "`Price Action | 1.0% | FOMO & Overtrading`"
)

HELP_TEXT = (
    "*Institutional Trading Coach — Commands*\n\n"
    "• `/log Pair | Setup | Risk% | RR | WIN/LOSS/BE | Notes` — Log a completed trade\n"
    "  Example: `/log XAUUSD | FVG Sweep | 1.0 | 3.0 | WIN | Swept liquidity cleanly`\n\n"
    "• `/stats` — View your trading performance metrics\n\n"
    "• `/reset` — Clear session memory & onboarding profile\n\n"
    "• `/help` — Show this command list\n\n"
    "Or just send a message for pre-trade checklists, trade analysis, or coaching."
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate onboarding flow on /start."""
    chat_id = update.effective_chat.id
    reset_chat_session(chat_id)
    
    existing_profile = get_user_profile(chat_id)
    if existing_profile:
        await update.message.reply_text(
            f"*Welcome back!* Your profile is active:\n\n{existing_profile}\n\n"
            "Send a trade setup or question, or `/reset` to update your profile.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(ONBOARDING_TEXT, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show command reference."""
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear session and profile."""
    chat_id = update.effective_chat.id
    reset_chat_session(chat_id)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM profiles WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        "🔄 *Profile and session cleared.*\n\n"
        "Send `/start` to onboard again, or just ask a question.",
        parse_mode=ParseMode.MARKDOWN
    )

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
        await update.message.reply_text(f"✅ *Trade Logged!* Pair: `{pair}` | Outcome: `{outcome}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("DB Error: %s", e)
        await update.message.reply_text("⚠️ Failed to log. Ensure Risk% and RR are numbers.")

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
        f"• *Total Trades:* {total}\n"
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

    # Check if user is providing onboarding answers (format: Strategy | Risk% | Weakness)
    if "|" in user_text and not get_user_profile(chat_id):
        parts = [p.strip() for p in user_text.split("|")]
        if len(parts) >= 3:
            strategy, risk, weakness = parts[0], parts[1], parts[2]
            profile_summary = f"Strategy: {strategy}\nMax Risk: {risk}\nWeakness: {weakness}"
            save_user_profile(chat_id, profile_summary)
            reset_chat_session(chat_id)  # Reset to reload with new profile in system instruction
            
            await update.message.reply_text(
                f"✅ *Profile saved!*\n\n{profile_summary}\n\n"
                "Now send me your trade setups or questions for adaptive coaching.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("log", log_trade_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting with adaptive pre-trade checklist...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()