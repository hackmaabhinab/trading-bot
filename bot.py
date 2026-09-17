"""
Institutional Trading Coach — Telegram Bot
==========================================
Block 2: Supabase PostgreSQL Connected Engine
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from supabase import create_client, Client
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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("Missing required environment variables (Telegram, Gemini, or Supabase).")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SYSTEM_INSTRUCTION = (
    "You are a strict, world-class Institutional Risk Manager & Trading Coach specializing in SMC, ICT, and Gold (XAUUSD).\n\n"
    
    "CORE WORKFLOW & BEHAVIOR:\n"
    "1. FIRST CONVERSATION / ORGANIC ONBOARDING: When the user sends their first message or setup, ALWAYS respond directly to their query first. "
    "Then, seamlessly ask them 3 concise baseline questions at the end of your response to extract their profile:\n"
    "   - What is their Primary Strategy (e.g., SMC/ICT, Price Action, Trend)?\n"
    "   - What is their Max Risk Per Trade (e.g., 0.5% or 1.0%)?\n"
    "   - What is their Primary Psychological Flaw (e.g., FOMO, Revenge Trading, Overtrading)?\n\n"
    
    "2. PROFILE LOCK: Once they reply with their details, acknowledge and lock these parameters into memory as their 'Trader Profile'.\n\n"
    
    "3. ADAPTIVE PRE-TRADE AUDIT: Once their profile is known, whenever they share a trade setup, run a strict, personalized 4-step checklist:\n"
    "   - [Step 1: High-Impact News]: High-impact macro events cleared?\n"
    "   - [Step 2: Strategy Confluence]: Does setup fit THEIR specific strategy rules?\n"
    "   - [Step 3: Risk Parameter]: Position risk within their declared max limit?\n"
    "   - [Step 4: Psychology Check]: Is this trade execution free from their stated psychological flaw?\n\n"
    
    "4. TONALITY: Direct, institutional, authoritative, and concise."
)

TELEGRAM_MESSAGE_LIMIT = 4096
TYPING_REFRESH_SECONDS = 4

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("trading_coach_bot")

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
                temperature=0.3,
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
# Handlers & Commands
# --------------------------------------------------------------------------

WELCOME_TEXT = (
    "Institutional Trading Coach — Online.\n\n"
    "Commands available:\n"
    "• `/log Pair | Setup | Risk% | RR | Outcome | Notes` — Log a trade\n"
    "• `/stats` — View your personal performance & metrics\n"
    "• `/reset` — Clear active conversation memory\n"
    "• `/help` — Show available commands"
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_chat_session(update.effective_chat.id)
    await update.message.reply_text(WELCOME_TEXT)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_chat_session(update.effective_chat.id)
    await update.message.reply_text("🔄 Session cleared. Send any message to begin fresh.")

async def log_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split("|")]

    if len(parts) < 5:
        await update.message.reply_text(
            "⚠️ *Invalid Format!*\nUse: `/log Pair | Setup | Risk% | RR | WIN/LOSS/BE | Notes`\n"
            "Example:\n`/log XAUUSD | FVG Sweep | 1.0 | 3.0 | WIN | Swept Asian high`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pair, setup, risk_pct, rr, outcome = parts[0], parts[1], parts[2], parts[3], parts[4].upper()
    notes = parts[5] if len(parts) > 5 else "None"

    try:
        # Insert trade record directly into Supabase PostgreSQL database
        trade_data = {
            "telegram_id": user.id,
            "pair": pair,
            "setup_type": setup,
            "risk_pct": float(risk_pct),
            "rr_ratio": float(rr),
            "outcome": outcome,
            "notes": notes,
        }
        supabase.table("trades").insert(trade_data).execute()
        await update.message.reply_text(f"✅ *Trade Logged to Supabase Cloud!* Pair: `{pair}` | Outcome: `{outcome}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("Supabase DB Error: %s", e)
        await update.message.reply_text("⚠️ Failed to log to cloud database. Ensure Risk% and RR are numbers.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        response = supabase.table("trades").select("*").eq("telegram_id", user_id).execute()
        trades = response.data

        if not trades:
            await update.message.reply_text("No trades logged yet. Use `/log` to add your first trade!")
            return

        total = len(trades)
        wins = sum(1 for t in trades if t["outcome"] == "WIN")
        losses = sum(1 for t in trades if t["outcome"] == "LOSS")
        be = sum(1 for t in trades if t["outcome"] == "BE")
        
        avg_risk = sum(float(t["risk_pct"]) for t in trades) / total
        avg_rr = sum(float(t["rr_ratio"]) for t in trades) / total
        win_rate = (wins / total * 100) if total > 0 else 0

        msg = (
            f"📊 *Your Cloud Metrics (Supabase)*\n\n"
            f"• *Total Trades:* {total}\n"
            f"• *Win Rate:* {win_rate:.1f}%\n"
            f"• *Wins:* {wins} | *Losses:* {losses} | *BE:* {be}\n"
            f"• *Avg Risk/Trade:* {avg_risk:.2f}%\n"
            f"• *Avg RR Ratio:* {avg_rr:.2f}R"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("Supabase Stats Error: %s", e)
        await update.message.reply_text("⚠️ Error fetching performance metrics from Supabase.")

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        await update.message.reply_text("Unauthorized.")
        return

    try:
        response = supabase.table("trades").select("*").execute()
        trades = response.data

        if not trades:
            await update.message.reply_text("No user data recorded yet in Supabase.")
            return

        # Group stats by user
        user_stats: Dict[int, Dict[str, Any]] = {}
        for t in trades:
            tid = t["telegram_id"]
            if tid not in user_stats:
                user_stats[tid] = {"total": 0, "wins": 0}
            user_stats[tid]["total"] += 1
            if t["outcome"] == "WIN":
                user_stats[tid]["wins"] += 1

        report = "👑 *Admin Overview (Supabase Cloud)*\n\n"
        for tid, data in user_stats.items():
            wr = (data["wins"] / data["total"] * 100) if data["total"] > 0 else 0
            report += f"• *ID `{tid}`*: {data['total']} trades | Win Rate: {wr:.1f}%\n"

        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("Supabase Admin Error: %s", e)
        await update.message.reply_text("⚠️ Error fetching global analytics.")

# --------------------------------------------------------------------------
# Message Handlers
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
        reply_text = "⚠️ AI Backend error. Please try again."
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

    logger.info("Bot online with Supabase cloud database active...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()