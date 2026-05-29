"""Telegram bot: Ярослав Вомитов persona (long polling, Railway worker)."""

from __future__ import annotations

import logging
import os
from collections import deque

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from prompt_loader import build_system_prompt

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vomitbot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "15"))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

SKIP_MARKERS = ("[SKIP]", "[SILENCE]", "SKIP", "")

context_store: dict[int, deque[str]] = {}
SYSTEM_PROMPT = build_system_prompt()
_openai_client: OpenAI | None = None


def get_openai_client() -> OpenAI | None:
    global _openai_client
    if not OPENAI_API_KEY:
        return None
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def get_context(chat_id: int) -> deque[str]:
    if chat_id not in context_store:
        context_store[chat_id] = deque(maxlen=CONTEXT_WINDOW)
    return context_store[chat_id]


def add_message(chat_id: int, name: str, text: str) -> None:
    ctx = get_context(chat_id)
    ctx.append(f"{name}: {text}")


def display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Аноним"
    return (user.full_name or user.first_name or "Аноним").strip()


def should_reply(update: Update, bot_username: str | None) -> bool:
    message = update.effective_message
    if not message or not message.text:
        return False

    chat = update.effective_chat
    if not chat:
        return False

    if chat.type == ChatType.PRIVATE:
        return True

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.is_bot:
            return True

    if bot_username and message.text:
        handle = f"@{bot_username.lower()}"
        if handle in message.text.lower():
            return True
        if message.entities:
            for entity in message.entities:
                if entity.type == "mention":
                    mention = message.text[entity.offset : entity.offset + entity.length]
                    if mention.lower() == handle:
                        return True

    return False


def format_user_payload(chat_id: int, current_name: str, current_text: str) -> str:
    history = list(get_context(chat_id))
    history_block = "\n".join(history) if history else "(пусто)"
    return (
        f"ИСТОРИЯ ЧАТА (последние {CONTEXT_WINDOW} сообщений):\n"
        f"{history_block}\n\n"
        f"ТЕКУЩЕЕ СООБЩЕНИЕ, НА КОТОРОЕ НУЖНО ОТВЕТИТЬ:\n"
        f"{current_name}: {current_text}"
    )


def generate_reply(chat_id: int, current_name: str, current_text: str) -> str | None:
    client = get_openai_client()
    if not client:
        logger.error("OPENAI_API_KEY is not set")
        return "ладно признаюсь мозги в облаке а ключей нет"

    user_content = format_user_payload(chat_id, current_name, current_text)

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.9,
            max_tokens=400,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        user_content
                        + "\n\n"
                        "Ответь только текстом реплики. "
                        "Если сообщение — чистый троллинг/спам/провокация без смысла, "
                        "верни ровно [SKIP] и ничего больше."
                    ),
                },
            ],
        )
    except Exception:
        logger.exception("OpenAI request failed")
        return "что то сломалось в нейронке. потом попробуй"

    reply = (response.choices[0].message.content or "").strip()
    if reply in SKIP_MARKERS or reply.upper() == "[SKIP]":
        return None
    return reply


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    chat = update.effective_chat
    if not chat:
        return

    bot_username = context.bot.username
    name = display_name(update)
    text = message.text.strip()
    chat_id = chat.id

    add_message(chat_id, name, text)

    if not should_reply(update, bot_username):
        return

    reply = generate_reply(chat_id, name, text)
    if not reply:
        logger.info("Skipped reply in chat %s", chat_id)
        return

    add_message(chat_id, "Ярослав Вомитов", reply)
    await message.reply_text(reply)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is required")

    try:
        get_openai_client()
        openai_ok = bool(OPENAI_API_KEY)
    except Exception:
        logger.exception("OpenAI client init failed")
        openai_ok = False

    logger.info(
        "Starting bot (context=%s, model=%s, openai=%s, token=%s)",
        CONTEXT_WINDOW,
        OPENAI_MODEL,
        "ok" if openai_ok else "missing",
        "set" if TELEGRAM_TOKEN else "missing",
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
