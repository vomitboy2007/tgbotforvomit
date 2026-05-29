"""Telegram bot for the VOMITBOY.COM persona.

The bot runs through long polling, so Railway should start it as a worker.
"""

from __future__ import annotations

import logging
import os
import random
from collections import deque

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from prompt_loader import build_system_prompt

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
for noisy_logger in ("httpx", "httpcore", "openai", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger("vomitbot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "15"))

SKIP_MARKERS = {"", "SKIP", "[SKIP]", "[SILENCE]"}
LORE_URL = "https://vomitboycom.neocities.org/"
LORE_FACTS = (
    "vomitboy на сайте описан как российская андеграундная субкультура начала 2020-х для людей, которым тесно в мейнстриме",
    "DIET 13 это не диета, а кулинарная яма с Горячей Штучкой, Левушкой детям, Крым Energy, свиными ушами и майонезом",
    "основа культуры вомитбоев это нетсталкинг, карты с находками, аниме и видеоигры",
    "полевой журнал FIELD LOG устроен как заметки с датой, координатами и фото, потому что если нашел странное место - оставь координаты",
    "маскотами были Рыгоша-подсолнух, Creepy Hatsune Miku doll и кружка с Микки Маусом, а актуальная икона это VOMIT GF с глазами //",
    "в летописи сайта есть 14.11.2022 как падение дискорд сервера, 15.07.2023 как вомит сходка и 06.02.2026-now как still breathing",
    "на сайте прямо написано don't ask questions. don't explain. remember that you are a biorobot",
    "визуальные мотивы вомитбоя это гнилая еда, старые вещи, грязные кружки, мусор, геотеги, нетсталкинг и старый интернет",
)

context_store: dict[int, deque[str]] = {}
SYSTEM_PROMPT = build_system_prompt()
_openai_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI | None:
    global _openai_client

    if not OPENAI_API_KEY:
        return None

    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    return _openai_client


def get_context(chat_id: int) -> deque[str]:
    if chat_id not in context_store:
        context_store[chat_id] = deque(maxlen=CONTEXT_WINDOW)
    return context_store[chat_id]


def add_message(chat_id: int, name: str, text: str) -> None:
    get_context(chat_id).append(f"[{name}]: {text}")


def display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Аноним"
    return (user.full_name or user.first_name or "Аноним").strip()


def should_reply(update: Update, bot_username: str | None, bot_id: int | None) -> bool:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text or not chat:
        return False

    if chat.type == ChatType.PRIVATE:
        return True

    reply = message.reply_to_message
    if reply and reply.from_user:
        reply_user = reply.from_user
        if bot_id is not None and reply_user.id == bot_id:
            return True
        if bot_username and reply_user.username:
            if reply_user.username.lower() == bot_username.lower():
                return True

    if not bot_username:
        return False

    bot_handle = f"@{bot_username.lower()}"
    text = message.text.lower()
    if bot_handle in text:
        return True

    for entity in message.entities or ():
        if entity.type != "mention":
            continue
        mention = message.text[entity.offset : entity.offset + entity.length]
        if mention.lower() == bot_handle:
            return True

    return False


def format_user_payload(chat_id: int, current_name: str, current_text: str) -> str:
    history = list(get_context(chat_id))
    history_block = "\n".join(history) if history else "(пусто)"

    return (
        f"ИСТОРИЯ ЧАТА (последние {CONTEXT_WINDOW} сообщений):\n"
        f"{history_block}\n\n"
        "ТЕКУЩЕЕ СООБЩЕНИЕ, НА КОТОРОЕ НУЖНО ОТВЕТИТЬ:\n"
        f"[{current_name}]: {current_text}"
    )


def is_skip_reply(reply: str) -> bool:
    normalized = reply.strip().strip('"').strip("'").upper()
    return normalized in SKIP_MARKERS


def build_lore_reply() -> str:
    fact = random.choice(LORE_FACTS)
    return f"а ты знал, что {fact}, чекни - {LORE_URL}"


async def generate_reply(chat_id: int, current_name: str, current_text: str) -> str | None:
    client = get_openai_client()
    if not client:
        logger.error("OPENAI_API_KEY is not set")
        return "ладно признаюсь мозги в облаке а ключей нет"

    user_payload = format_user_payload(chat_id, current_name, current_text)

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.9,
            max_tokens=400,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"{user_payload}\n\n"
                        "Ответь только текстом реплики для Telegram. "
                        "Если сообщение является чистым троллингом, спамом или пустой провокацией, "
                        "верни ровно [SKIP] и ничего больше."
                    ),
                },
            ],
        )
    except Exception:
        logger.exception("OpenAI request failed")
        return "что то сломалось в нейронке. потом попробуй"

    reply = (response.choices[0].message.content or "").strip()
    if is_skip_reply(reply):
        return None

    return reply


async def on_lore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    name = display_name(update)
    text = message.text.strip() if message.text else "/lore"
    reply = build_lore_reply()

    add_message(chat.id, name, text)
    add_message(chat.id, "Ярослав Вомитов", reply)
    await message.reply_text(reply)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text or not chat:
        return

    name = display_name(update)
    text = message.text.strip()
    chat_id = chat.id
    bot_username = context.bot.username
    bot_id = getattr(context.bot, "id", None)

    reply_needed = should_reply(update, bot_username, bot_id)
    reply = await generate_reply(chat_id, name, text) if reply_needed else None

    add_message(chat_id, name, text)

    if not reply:
        if reply_needed:
            logger.info("Skipped reply in chat %s", chat_id)
        return

    add_message(chat_id, "Ярослав Вомитов", reply)
    await message.reply_text(reply)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is required")

    logger.info(
        "Starting bot (context=%s, model=%s, openai=%s, token=%s)",
        CONTEXT_WINDOW,
        OPENAI_MODEL,
        "set" if OPENAI_API_KEY else "missing",
        "set" if TELEGRAM_TOKEN else "missing",
    )

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("lore", on_lore))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
