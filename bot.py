"""
Institutional Trading Coach — Telegram Bot
===========================================
Persona: a strict SMC / ICT / Gold (XAUUSD) trading discipline coach.

Stack:
  - python-telegram-bot  (v22, async)
  - google-genai          (Gemini Developer API, gemini-3.6-flash)

Run:
  python bot.py

Credentials are read from environment variables (see .env.example) —
never hardcode a bot token or API key directly in this file. See the
README for why, and for how to rotate a key that has been exposed.
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
# Configuration
# --------------------------------------------------------------------------

load_dotenv()  # reads a local .env file, if present

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

if not TELEGRAM_BOT_TOKEN:
    sys.exit("Missing TELEGRAM_BOT_TOKEN. Set it in your .env file (see .env.example).")
if not GEMINI_API_KEY:
    sys.exit("Missing GEMINI_API_KEY. Set it in your .env file (see .env.example).")

SYSTEM_INSTRUCTION = (
    "You are an expert Institutional Trading Coach specializing in SMC "
    "(Smart Money Concepts), ICT, and Gold (XAUUSD). Your job is to act as "
    "a strict, professional personal coach. Help traders pre-check their "
    "risk management, analyze their trade journals, prevent emotional "
    "revenge trading, and enforce discipline. Keep answers sharp, "
    "practical, and direct."
)

TELEGRAM_MESSAGE_LIMIT = 4096
TYPING_REFRESH_SECONDS = 4  # Telegram's "typing…" indicator fades after ~5s

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # quiet noisy libraries
logger = logging.getLogger("trading_coach_bot")

# --------------------------------------------------------------------------
# Gemini client + per-chat conversation sessions
# --------------------------------------------------------------------------

genai_client = genai.Client(api_key=GEMINI_API_KEY)

# One multi-turn chat session per Telegram chat, so the coach keeps context
_chat_sessions: Dict[int, Any] = {}  # values are google.genai async Chat objects


def get_chat_session(chat_id: int) -> Any:
    session = _chat_sessions.get(chat_id)
    if session is None:
        session = genai_client.aio.chats.create(
            model=GEMINI_MODEL,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.4,  # lower temperature: consistent, disciplined tone
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
    """Yield chunks of `text` no longer than `limit` chars, breaking on
    paragraph/line boundaries where possible so formatting stays intact."""
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
    """Send `text` in Telegram-safe chunks. Tries Markdown first (Gemini's
    output is usually Markdown-flavored); falls back to plain text if a
    chunk isn't valid Telegram Markdown."""
    for chunk in split_message(text):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.message.reply_text(chunk)


async def _keep_typing(bot, chat_id: int, stop_event: asyncio.Event) -> None:
    """Refreshes the 'typing…' indicator every few seconds until stop_event
    is set, since a single send_chat_action call only lasts ~5 seconds and
    Gemini calls can take longer than that."""
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
# Command handlers
# --------------------------------------------------------------------------

WELCOME_TEXT = (
    "*Institutional Trading Coach* — online.\n\n"
    "Send me your trade plan, a journal entry, or a question on SMC / ICT / "
    "XAUUSD execution and I'll give it to you straight — no hand-holding.\n\n"
    "*Commands*\n"
    "/reset — clear this chat's conversation memory\n"
    "/help — show this message again"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_chat_session(update.effective_chat.id)
    await update.message.reply_text("Context cleared. Starting fresh — what's the setup?")


# --------------------------------------------------------------------------
# Main message handler
# --------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _keep_typing(context.bot, chat_id, stop_typing)
    )

    try:
        session = get_chat_session(chat_id)
        response = await session.send_message(user_text)
        reply_text = (response.text or "").strip() or (
            "I didn't get a usable response for that — try rephrasing "
            "with more detail (pair, timeframe, what you're seeing)."
        )
    except genai_errors.APIError as exc:
        logger.error("Gemini API error: %s", exc)
        reply_text = (
            "⚠️ The coach's AI backend is temporarily unavailable "
            "(API error). Try again in a moment."
        )
    except Exception:
        logger.exception("Unexpected error while handling message")
        reply_text = "⚠️ Something went wrong on my end. Try again."
    finally:
        stop_typing.set()
        await typing_task

    await send_long_message(update, reply_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    logger.info("Institutional Trading Coach bot starting (polling)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()