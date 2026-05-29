"""Telegram bot for the VOMITBOY.COM persona.

The bot runs through long polling, so Railway should start it as a worker.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from math import sqrt
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Message, Update
from telegram.constants import ChatType, MessageEntityType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from prompt_loader import build_system_prompt
from site_lore import LORE_URL, SITE_LORE
from web_search import format_search_context, search_web

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
for noisy_logger in ("httpx", "httpcore", "openai", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger("vomitbot")


def read_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Invalid integer for %s=%r, using %s", name, raw, default)
            value = default

    if minimum is not None:
        value = max(minimum, value)

    return value


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate_text(text: str, limit: int) -> str:
    cleaned = normalize_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in TOKEN_RE.findall(normalize_text(text).lower()):
        if len(token) > 2 or token.isdigit():
            tokens.add(token)
    return tokens


def similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0

    overlap = len(left & right)
    if not overlap:
        return 0.0

    return overlap / sqrt(len(left) * len(right))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


ROOT = Path(__file__).resolve().parent
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
CONTEXT_WINDOW = read_int_env("CONTEXT_WINDOW", 15, minimum=1)
LEARNING_STORE_PATH = Path(
    os.environ.get("LEARNING_STORE_PATH", str(ROOT / "runtime" / "learned_turns.jsonl"))
)
LEARNING_LIMIT = read_int_env("LEARNING_LIMIT", 3, minimum=1)
LEARNING_STORE_MAX = read_int_env("LEARNING_STORE_MAX", 500, minimum=50)
IMAGE_MAX_BYTES = read_int_env("IMAGE_MAX_BYTES", 8 * 1024 * 1024, minimum=1024 * 1024)

SKIP_MARKERS = {"", "SKIP", "[SKIP]", "[SILENCE]"}
BOT_DISPLAY_NAME = "Ярослав Вомитов"
FALLBACK_NO_API_REPLY = "ладно признаюсь мозги в облаке а ключей нет"
FALLBACK_ERROR_REPLY = "что то сломалось в нейронке. потом попробуй"
REPLY_INSTRUCTIONS = (
    "Ответь только текстом реплики для Telegram. "
    "Пиши строчными буквами, каждое предложение заканчивай точкой. "
    "Не используй брат, братан, бро и похожую фамильярность. "
    "Не будь излишне любезным и восхищённым. "
    "Если не хватает фактов, верни только одну строку [SEARCH: запрос]. "
    "Если сообщение является чистым троллингом, спамом или пустой провокацией, верни ровно [SKIP] и ничего больше. "
    "Если сообщение содержит фото, картинку или скриншот, сначала разберись, что видно на изображении, и комментируй это как живой участник сообщества, без канцелярита. "
    "Если деталей не видно, честно скажи, что изображение мутное, обрезано или не читается."
)
SEARCH_REQUEST_RE = re.compile(r"^\[SEARCH:\s*(.+?)\s*\]\s*$", re.IGNORECASE | re.DOTALL)
FAMILIARITY_RE = re.compile(r"\b(братанчик|братан|брат|бро)\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+", re.UNICODE)
BOT_DATA_ID_KEY = "bot_id"
BOT_DATA_USERNAME_KEY = "bot_username"
INCOMING_CONTENT = (
    filters.TEXT
    | filters.CAPTION
    | filters.PHOTO
    | filters.Document.IMAGE
) & ~filters.COMMAND

context_store: dict[int, deque[str]] = {}
SYSTEM_PROMPT = build_system_prompt()
_openai_client: AsyncOpenAI | None = None


@dataclass(slots=True)
class AttachmentInfo:
    kind: str
    mime_type: str
    note: str
    data_url: str | None
    file_name: str | None = None


@dataclass(slots=True)
class LearningExample:
    chat_id: int
    query: str
    reply: str
    kind: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LearningExample":
        query = normalize_text(str(payload.get("query", "")))
        reply = normalize_text(str(payload.get("reply", "")))
        if not query or not reply:
            raise ValueError("missing query or reply")

        chat_id_raw = payload.get("chat_id", 0)
        try:
            chat_id = int(chat_id_raw or 0)
        except (TypeError, ValueError):
            chat_id = 0

        kind = normalize_text(str(payload.get("kind", "text"))) or "text"
        created_at = normalize_text(str(payload.get("created_at", ""))) or now_iso()

        return cls(
            chat_id=chat_id,
            query=truncate_text(query, 240),
            reply=truncate_text(reply, 400),
            kind=kind,
            created_at=created_at,
        )


@dataclass(slots=True)
class ReplyOutcome:
    text: str | None
    learnable: bool
    query: str
    kind: str


class LearningMemory:
    def __init__(self, path: Path, *, max_entries: int = 500) -> None:
        self.path = path
        self.max_entries = max_entries
        self.entries = self._load()

    def _load(self) -> list[LearningExample]:
        if not self.path.is_file():
            return []

        loaded: list[LearningExample] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    try:
                        example = LearningExample.from_dict(payload)
                    except (TypeError, ValueError):
                        continue
                    loaded.append(example)
        except OSError:
            logger.exception("Failed to load learning memory from %s", self.path)
            return []

        return loaded[-self.max_entries :]

    def _write_all(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(
            json.dumps(example.to_dict(), ensure_ascii=False) for example in self.entries
        )
        if payload:
            payload += "\n"
        self.path.write_text(payload, encoding="utf-8")

    def record(self, chat_id: int, query: str, reply: str, *, kind: str) -> None:
        query = normalize_text(query)
        reply = normalize_text(reply)
        if not query or not reply or is_skip_reply(reply):
            return

        example = LearningExample(
            chat_id=chat_id,
            query=truncate_text(query, 240),
            reply=truncate_text(reply, 400),
            kind=kind or "text",
            created_at=now_iso(),
        )
        self.entries.append(example)
        self.entries = self.entries[-self.max_entries :]

        try:
            self._write_all()
        except OSError:
            logger.exception("Failed to persist learning memory to %s", self.path)

    def related_examples(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 3,
    ) -> list[LearningExample]:
        normalized_query = normalize_text(query)
        query_tokens = tokenize(normalized_query)
        if not self.entries:
            return []

        scored: list[tuple[float, int, LearningExample]] = []
        for index, example in enumerate(self.entries):
            example_query = normalize_text(example.query)
            if normalized_query and example_query.lower() == normalized_query.lower():
                continue

            score = similarity(query_tokens, tokenize(example_query))
            if normalized_query and not score:
                if normalized_query.lower() in example_query.lower() or example_query.lower() in normalized_query.lower():
                    score = 0.35

            if kind:
                if example.kind == kind:
                    score += 0.12
                else:
                    score *= 0.7

            if score > 0:
                scored.append((score, index, example))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [example for _, _, example in scored[:limit]]


LEARNING_MEMORY = LearningMemory(LEARNING_STORE_PATH, max_entries=LEARNING_STORE_MAX)


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


def render_chat_line(name: str, text: str, *, attachment_note: str | None = None) -> str:
    body = normalize_text(text) or "[пусто]"
    lines = [f"[{name}]: {body}"]
    if attachment_note:
        lines.append(f"[медиа]: {attachment_note}")
    return "\n".join(lines)


def add_message(chat_id: int, name: str, text: str, *, attachment_note: str | None = None) -> None:
    get_context(chat_id).append(render_chat_line(name, text, attachment_note=attachment_note))


def display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Аноним"
    return normalize_text(user.full_name or user.first_name or "Аноним") or "Аноним"


def get_message_text(message: object) -> str:
    text = getattr(message, "text", None)
    if text:
        return text
    caption = getattr(message, "caption", None)
    if caption:
        return caption
    return ""


def get_message_entities(message: object) -> tuple[object, ...]:
    text = getattr(message, "text", None)
    if text:
        return tuple(getattr(message, "entities", ()) or ())

    caption = getattr(message, "caption", None)
    if caption:
        return tuple(getattr(message, "caption_entities", ()) or ())

    return ()


def has_image_attachment(message: object) -> bool:
    photo = getattr(message, "photo", None)
    if photo:
        return True

    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", "") or ""
    return bool(document and mime_type.startswith("image/"))


def build_learning_query(current_text: str, attachment: AttachmentInfo | None) -> str:
    base = normalize_text(current_text)
    if attachment:
        return normalize_text(f"{base} {attachment.note}") or attachment.note or attachment.kind
    return base


def build_learning_block(examples: list[LearningExample]) -> str:
    if not examples:
        return ""

    lines = ["ПАМЯТЬ О ПОХОЖИХ УДАЧНЫХ ОТВЕТАХ:"]
    for example in examples:
        lines.append(f"- Вход: {truncate_text(example.query, 180)}")
        lines.append(f"  Ответ: {truncate_text(example.reply, 220)}")
    return "\n".join(lines)


def format_user_payload(
    chat_id: int,
    current_name: str,
    current_text: str,
    *,
    attachment_note: str | None = None,
    learned_examples: list[LearningExample] | None = None,
) -> str:
    history = list(get_context(chat_id))
    history_block = "\n".join(history) if history else "(пусто)"
    current_block = render_chat_line(current_name, current_text, attachment_note=attachment_note)
    learning_block = build_learning_block(learned_examples or [])

    parts = [
        f"ИСТОРИЯ ЧАТА (последние {CONTEXT_WINDOW} сообщений):",
        history_block,
    ]
    if learning_block:
        parts.extend(["", learning_block])
    parts.extend(
        [
            "",
            "ТЕКУЩЕЕ СООБЩЕНИЕ, НА КОТОРОЕ НУЖНО ОТВЕТИТЬ:",
            current_block,
        ]
    )
    return "\n".join(parts)


def is_skip_reply(reply: str) -> bool:
    normalized = reply.strip().strip('"').strip("'").upper()
    return normalized in SKIP_MARKERS


def parse_search_request(reply: str) -> str | None:
    match = SEARCH_REQUEST_RE.match(reply.strip())
    if not match:
        return None
    query = normalize_text(match.group(1))
    return query or None


def apply_style_rules(reply: str) -> str:
    if is_skip_reply(reply):
        return reply

    text = FAMILIARITY_RE.sub("", reply)
    text = re.sub(r"[\s,;:]+", " ", text)
    text = normalize_text(text).lower()
    if not text:
        return text

    if text[-1] not in ".!?…":
        text += "."
    return text


def entity_type_name(entity: object) -> str:
    entity_type = getattr(entity, "type", None)
    if entity_type is None:
        return ""
    return str(getattr(entity_type, "value", entity_type)).lower()


def get_bot_identity(context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str | None]:
    bot_data = context.application.bot_data
    bot_id = bot_data.get(BOT_DATA_ID_KEY)
    bot_username = bot_data.get(BOT_DATA_USERNAME_KEY) or context.bot.username

    if bot_id is None:
        bot_id = getattr(context.bot, "id", None)

    if bot_username:
        bot_username = bot_username.lstrip("@").lower()

    return bot_id, bot_username


def build_group_trigger_filter(bot_id: int, bot_username: str | None) -> filters.BaseFilter:
    """Match group messages that reply to the bot or @mention it (Telegram privacy-safe)."""
    mentions: list[int | str] = [bot_id]
    if bot_username:
        mentions.append(bot_username.lstrip("@"))

    return filters.ChatType.GROUPS & INCOMING_CONTENT & (
        filters.REPLY | filters.Mention(mentions)
    )


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data[BOT_DATA_ID_KEY] = me.id
    application.bot_data[BOT_DATA_USERNAME_KEY] = _normalize_username(me.username)
    logger.info("Bot ready: id=%s username=@%s", me.id, me.username)

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & INCOMING_CONTENT,
            handle_chat_message,
        ),
        group=0,
    )
    application.add_handler(
        MessageHandler(
            build_group_trigger_filter(me.id, me.username),
            handle_chat_message,
        ),
        group=0,
    )


def build_lore_reply(chat_id: int) -> tuple[str, str]:
    entry = SITE_LORE.pick_for_command(chat_id)
    if not entry:
        fallback = (
            f"сайт {LORE_URL} лежит, но база фактов пустая. перезалей data/site_lore.json."
        )
        return apply_style_rules(fallback), f"<blockquote>{escape(fallback)}</blockquote>"

    plain_reply, html_reply = SITE_LORE.format_command_reply(entry)
    return apply_style_rules(plain_reply), html_reply


def _normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    return username.lstrip("@").lower()


def _message_entities(message: Message) -> tuple[object, ...]:
    return tuple(message.entities or ()) + tuple(message.caption_entities or ())


def is_reply_to_bot(message: Message, bot_id: int | None, bot_username: str | None) -> bool:
    reply = message.reply_to_message
    if not reply:
        return False

    reply_user = reply.from_user
    if not reply_user:
        return False

    if bot_id is not None and reply_user.id == bot_id:
        return True

    reply_username = _normalize_username(reply_user.username)
    bot_name = _normalize_username(bot_username)
    if reply_user.is_bot and bot_name and reply_username == bot_name:
        return True

    return False


def is_mention_to_bot(message: Message, bot_id: int | None, bot_username: str | None) -> bool:
    body = get_message_text(message)
    bot_name = _normalize_username(bot_username)

    if body and bot_name:
        lowered = body.lower()
        if f"@{bot_name}" in lowered:
            return True

    for entity in _message_entities(message):
        entity_name = entity_type_name(entity)

        if entity_name == MessageEntityType.TEXT_MENTION.value and bot_id is not None:
            mentioned_user = getattr(entity, "user", None)
            if mentioned_user and mentioned_user.id == bot_id:
                return True
            continue

        if entity_name != MessageEntityType.MENTION.value or not bot_name:
            continue

        try:
            fragment = _normalize_username(message.parse_entity(entity))
        except (RuntimeError, ValueError, IndexError, AttributeError):
            fragment = None

        if fragment == bot_name:
            return True

    return False


def should_reply(message: Message, bot_username: str | None, bot_id: int | None) -> bool:
    chat = getattr(message, "chat", None)
    if not message or not chat:
        return False

    body = normalize_text(get_message_text(message))
    attachment_present = has_image_attachment(message)
    if not body and not attachment_present:
        return False

    chat_type = getattr(chat, "type", None)
    if chat_type == ChatType.PRIVATE:
        return True

    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if is_reply_to_bot(message, bot_id, bot_username):
            return True
        if is_mention_to_bot(message, bot_id, bot_username):
            return True
        return False

    return False


async def extract_image_attachment(message: object) -> AttachmentInfo | None:
    photo = getattr(message, "photo", None)
    if photo:
        photo_size = photo[-1]
        mime_type = "image/jpeg"
        note = "фото"
        width = getattr(photo_size, "width", None)
        height = getattr(photo_size, "height", None)
        if width and height:
            note = f"фото {width}x{height}"

        file_size = getattr(photo_size, "file_size", None)
        if file_size and file_size > IMAGE_MAX_BYTES:
            return AttachmentInfo(
                kind="photo",
                mime_type=mime_type,
                note=f"{note} слишком большое для загрузки",
                data_url=None,
            )

        file = await photo_size.get_file()
        buffer = BytesIO()
        await file.download_to_memory(buffer)
        data = buffer.getvalue()
        if len(data) > IMAGE_MAX_BYTES:
            return AttachmentInfo(
                kind="photo",
                mime_type=mime_type,
                note=f"{note} слишком большое для загрузки",
                data_url=None,
            )

        return AttachmentInfo(
            kind="photo",
            mime_type=mime_type,
            note=note,
            data_url=encode_data_url(data, mime_type),
        )

    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", "") or ""
    if document and mime_type.startswith("image/"):
        file_name = getattr(document, "file_name", None)
        note = f"изображение {file_name or mime_type}"

        file_size = getattr(document, "file_size", None)
        if file_size and file_size > IMAGE_MAX_BYTES:
            return AttachmentInfo(
                kind="document",
                mime_type=mime_type,
                note=f"{note} слишком большое для загрузки",
                data_url=None,
                file_name=file_name,
            )

        file = await document.get_file()
        buffer = BytesIO()
        await file.download_to_memory(buffer)
        data = buffer.getvalue()
        if len(data) > IMAGE_MAX_BYTES:
            return AttachmentInfo(
                kind="document",
                mime_type=mime_type,
                note=f"{note} слишком большое для загрузки",
                data_url=None,
                file_name=file_name,
            )

        return AttachmentInfo(
            kind="document",
            mime_type=mime_type,
            note=note,
            data_url=encode_data_url(data, mime_type),
            file_name=file_name,
        )

    return None


async def call_openai(
    user_content: str | list[dict[str, object]],
) -> str:
    client = get_openai_client()
    if not client:
        raise RuntimeError("OPENAI_API_KEY is not set")

    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.9,
        max_tokens=400,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return (response.choices[0].message.content or "").strip()


async def generate_reply(
    chat_id: int,
    current_name: str,
    current_text: str,
    *,
    attachment: AttachmentInfo | None = None,
) -> ReplyOutcome:
    query = build_learning_query(current_text, attachment)
    kind = "image" if attachment else "text"
    learned_examples = LEARNING_MEMORY.related_examples(query, kind=kind, limit=LEARNING_LIMIT)
    user_payload = format_user_payload(
        chat_id,
        current_name,
        current_text,
        attachment_note=attachment.note if attachment else None,
        learned_examples=learned_examples,
    )

    lore_entries = SITE_LORE.pick_context_entries(query, chat_id)
    lore_block = SITE_LORE.format_context_block(lore_entries)
    user_text = f"{user_payload}"
    if lore_block:
        user_text = f"{user_text}\n\n{lore_block}"
    user_text = f"{user_text}\n\n{REPLY_INSTRUCTIONS}"
    user_content: str | list[dict[str, object]]
    if attachment and attachment.data_url:
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": attachment.data_url, "detail": "low"}},
        ]
    else:
        if attachment:
            user_text = f"{user_text}\n\n[Медиа: {attachment.note}]"
        user_content = user_text

    if not get_openai_client():
        logger.error("OPENAI_API_KEY is not set")
        return ReplyOutcome(
            text=apply_style_rules(FALLBACK_NO_API_REPLY),
            learnable=False,
            query=query,
            kind=kind,
        )

    try:
        reply = await call_openai(user_content)
    except Exception:
        logger.exception("OpenAI request failed")
        return ReplyOutcome(
            text=apply_style_rules(FALLBACK_ERROR_REPLY),
            learnable=False,
            query=query,
            kind=kind,
        )

    if is_skip_reply(reply):
        return ReplyOutcome(text=None, learnable=False, query=query, kind=kind)

    search_query = parse_search_request(reply)
    if search_query:
        results = await search_web(search_query)
        search_block = format_search_context(results)
        follow_up = (
            f"{user_payload}\n\n"
            f"{search_block}\n\n"
            f"{REPLY_INSTRUCTIONS}\n"
            "Дай финальный ответ по фактам из поиска. Не возвращай [SEARCH] повторно."
        )
        follow_up_content: str | list[dict[str, object]]
        if isinstance(user_content, list):
            follow_up_content = [{"type": "text", "text": follow_up}, *user_content[1:]]
        else:
            follow_up_content = follow_up

        try:
            reply = await call_openai(follow_up_content)
        except Exception:
            logger.exception("OpenAI follow-up after search failed")
            return ReplyOutcome(
                text=apply_style_rules(FALLBACK_ERROR_REPLY),
                learnable=False,
                query=query,
                kind=kind,
            )

        if is_skip_reply(reply):
            return ReplyOutcome(text=None, learnable=False, query=query, kind=kind)

    return ReplyOutcome(
        text=apply_style_rules(reply),
        learnable=True,
        query=query,
        kind=kind,
    )


async def on_lore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    name = display_name(update)
    text = message.text.strip() if message.text else "/lore"
    plain_reply, html_reply = build_lore_reply(chat.id)

    add_message(chat.id, name, text)
    add_message(chat.id, BOT_DISPLAY_NAME, plain_reply)
    await message.reply_text(
        html_reply,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    user = update.effective_user
    if user and user.is_bot:
        return

    name = display_name(update)
    chat_id = chat.id
    bot_id, bot_username = get_bot_identity(context)
    reply_needed = should_reply(message, bot_username, bot_id)

    raw_text = normalize_text(get_message_text(message))
    attachment = await extract_image_attachment(message) if has_image_attachment(message) else None
    current_text = raw_text or ("[фото]" if attachment else "")

    if not current_text and not attachment and not reply_needed:
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and not reply_needed:
        logger.info(
            "Ignored group message chat=%s reply_to_bot=%s mention=%s text=%r",
            chat_id,
            is_reply_to_bot(message, bot_id, bot_username),
            is_mention_to_bot(message, bot_id, bot_username),
            (raw_text or "")[:80],
        )
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and reply_needed:
        logger.info(
            "Group trigger chat=%s mention=%s reply=%s user=%s",
            chat_id,
            is_mention_to_bot(message, bot_id, bot_username),
            is_reply_to_bot(message, bot_id, bot_username),
            name,
        )

    outcome = ReplyOutcome(
        text=None,
        learnable=False,
        query=build_learning_query(current_text, attachment),
        kind="image" if attachment else "text",
    )
    if reply_needed:
        outcome = await generate_reply(chat_id, name, current_text, attachment=attachment)

    add_message(
        chat_id,
        name,
        current_text or "[пусто]",
        attachment_note=attachment.note if attachment else None,
    )

    if not outcome.text:
        if reply_needed:
            logger.info("Skipped reply in chat %s", chat_id)
        return

    add_message(chat_id, BOT_DISPLAY_NAME, outcome.text)
    await message.reply_text(outcome.text)

    if outcome.learnable:
        LEARNING_MEMORY.record(chat_id, outcome.query, outcome.text, kind=outcome.kind)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is required")

    logger.info(
        "Starting bot (context=%s, model=%s, openai=%s, token=%s, memory=%s, lore=%s)",
        CONTEXT_WINDOW,
        OPENAI_MODEL,
        "set" if OPENAI_API_KEY else "missing",
        "set" if TELEGRAM_TOKEN else "missing",
        LEARNING_STORE_PATH,
        SITE_LORE.size,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("lore", on_lore))
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
