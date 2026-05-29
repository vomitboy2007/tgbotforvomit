"""Telegram bot for the VOMITBOY.COM persona.

The bot runs through long polling, so Railway should start it as a worker.
"""

from __future__ import annotations

import asyncio
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
from telegram.constants import ChatAction, ChatType, MessageEntityType, ParseMode
from telegram.error import Conflict
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
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "vomitbot-webhook").strip().strip("/") or "vomitbot-webhook"
HTTP_PORT = read_int_env("PORT", 8080, minimum=1)
OPENAI_TIMEOUT = read_int_env("OPENAI_TIMEOUT", 50, minimum=10)
REPLY_TIMEOUT = read_int_env("REPLY_TIMEOUT", 55, minimum=15)


def resolve_webhook_base_url() -> str | None:
    explicit = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
    if explicit:
        return explicit if explicit.startswith("https://") else f"https://{explicit}"

    static_url = os.environ.get("RAILWAY_STATIC_URL", "").strip().rstrip("/")
    if static_url:
        return static_url if static_url.startswith("https://") else f"https://{static_url}"

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"

    return None


def is_railway_runtime() -> bool:
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
    )


def use_webhook_mode() -> bool:
    force_poll = os.environ.get("USE_POLLING", "").strip().lower() in ("1", "true", "yes")
    if force_poll:
        return False

    force_webhook = os.environ.get("USE_WEBHOOK", "").strip().lower() in ("1", "true", "yes")
    base = resolve_webhook_base_url()
    if force_webhook:
        return bool(base)
    return bool(base)


WEBHOOK_BASE_URL = resolve_webhook_base_url()
USE_WEBHOOK = use_webhook_mode()

SKIP_MARKERS = {"", "SKIP", "[SKIP]", "[SILENCE]"}
BOT_DISPLAY_NAME = "Ярослав Вомитов"
FALLBACK_NO_API_REPLY = "ладно признаюсь мозги в облаке а ключей нет"
FALLBACK_ERROR_REPLY = "что то сломалось в нейронке. потом попробуй"
REPLY_INSTRUCTIONS = (
    "Ответь только текстом реплики для Telegram. "
    "Пиши строчными буквами, каждое предложение заканчивай точкой. "
    "Не используй брат, братан, бро и похожую фамильярность. "
    "Не будь излишне любезным и не лей воду. "
    "Отвечай прямо на вопрос, без загадок и философии. "
    "Если спрашивают ник или имя — возьми из строки СОБЕСЕДНИК, не выдумывай. "
    "Никогда не пиши пользователю [SEARCH], [search] и подобные теги — это служебная метка. "
    "Если не хватает фактов, верни только одну строку: [SEARCH: короткий запрос]. "
    "Если тебя позвали через @ или реплай — всегда отвечай текстом, не возвращай [SKIP]. "
    "На провокации и троллинг — короткий ответ в образе (сухо, с иронией), без морали и без [SKIP]. "
    "[SKIP] только если сообщение вообще не к тебе и это чистый спам без вопроса. "
    "Если сообщение содержит фото, картинку или скриншот, сначала разберись, что видно на изображении, и комментируй это как живой участник сообщества, без канцелярита. "
    "Если деталей не видно, честно скажи, что изображение мутное, обрезано или не читается."
)
SEARCH_REQUEST_STRICT_RE = re.compile(
    r"^\[SEARCH:\s*(.+?)\s*\]\s*\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)
SEARCH_REQUEST_LOOSE_RE = re.compile(
    r"^\[?\s*search\s*:?\s*(.+?)\s*\]?\s*\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)
SEARCH_INLINE_RE = re.compile(r"\[search\s*:?\s*([^\]]+)\]", re.IGNORECASE)
SEARCH_MARKER_RE = re.compile(r"\[search", re.IGNORECASE)
NICKNAME_QUESTION_RE = re.compile(
    r"(какой\s+(у\s+меня|мой)\s+ник|мой\s+ник|моё\s+имя|мое\s+имя|"
    r"как\s+меня\s+зовут|как\s+зовут|какое\s+имя|какой\s+ник)",
    re.IGNORECASE,
)
FACTUAL_USER_QUESTION_RE = re.compile(
    r"\b("
    r"как\s+умер|когда\s+умер|кто\s+такой|что\s+такое|почему|сколько|"
    r"где\s+находится|когда\s+родился|когда\s+умерла"
    r")\b",
    re.IGNORECASE,
)
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
# All non-command messages in groups (stickers/voice with reply to bot were dropped before).
GROUP_INCOMING = (
    filters.ChatType.GROUPS
    & filters.UpdateType.MESSAGES
    & ~filters.COMMAND
    & ~filters.StatusUpdate.ALL
)

# === Safety / hardening constants (added during senior review) ===
MAX_USER_TEXT_CHARS = 1800          # hard cap before we even touch the LLM (cost + injection control)
MIN_REPLY_INTERVAL_SEC = 2.0        # per-user cooldown inside a chat (prevents one person from spamming the whole group)
LEARNING_WRITE_DEBOUNCE_SEC = 1.5   # batch learning writes a bit

context_store: dict[int, deque[str]] = {}
context_lock = asyncio.Lock()
_last_user_reply_ts: dict[int, dict[int, float]] = {}   # chat_id -> {user_id: last_reply_time}
_last_reply_lock = asyncio.Lock()

# Learning writes are moved off the hot path via a queue + background writer task
_learning_write_queue: asyncio.Queue[tuple[int, str, str, str]] | None = None
_learning_writer_task: asyncio.Task[None] | None = None

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
    """Internal — prefer safe_snapshot_context() for cross-task safety."""
    if chat_id not in context_store:
        context_store[chat_id] = deque(maxlen=CONTEXT_WINDOW)
    return context_store[chat_id]


async def safe_snapshot_context(chat_id: int) -> list[str]:
    """Thread-safe snapshot for prompt building."""
    async with context_lock:
        ctx = get_context(chat_id)
        return list(ctx)  # shallow copy of current messages


def render_chat_line(name: str, text: str, *, attachment_note: str | None = None) -> str:
    body = normalize_text(text) or "[пусто]"
    lines = [f"[{name}]: {body}"]
    if attachment_note:
        lines.append(f"[медиа]: {attachment_note}")
    return "\n".join(lines)


def add_message(chat_id: int, name: str, text: str, *, attachment_note: str | None = None) -> None:
    get_context(chat_id).append(render_chat_line(name, text, attachment_note=attachment_note))


# === Hardening helpers (senior review) ===

def cap_user_text(text: str) -> str:
    """Hard truncate to protect against prompt bloat and cost attacks."""
    if not text:
        return ""
    cleaned = normalize_text(text)
    if len(cleaned) <= MAX_USER_TEXT_CHARS:
        return cleaned
    return cleaned[:MAX_USER_TEXT_CHARS].rstrip() + "… [обрезано]"


async def can_reply_now(chat_id: int, user_id: int | None) -> bool:
    """Per-user rate limit inside a chat.
    Prevents one heavy tester (e.g. the owner) from blocking everyone else in the group.
    """
    if user_id is None:
        user_id = 0  # anonymous / channel posts get a shared slot

    loop = asyncio.get_running_loop()
    now = loop.time()
    async with _last_reply_lock:
        per_chat = _last_user_reply_ts.setdefault(chat_id, {})
        last = per_chat.get(user_id, 0.0)
        if now - last < MIN_REPLY_INTERVAL_SEC:
            return False
        per_chat[user_id] = now
        return True


async def safe_get_context(chat_id: int) -> deque[str]:
    async with context_lock:
        return get_context(chat_id)  # still returns the live deque; mutations below are also locked


async def safe_add_message(chat_id: int, name: str, text: str, *, attachment_note: str | None = None) -> None:
    async with context_lock:
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
    history_snapshot: list[str] | None = None,
) -> str:
    history = history_snapshot if history_snapshot is not None else list(get_context(chat_id))
    history_block = "\n".join(history) if history else "(пусто)"
    current_block = render_chat_line(current_name, current_text, attachment_note=attachment_note)
    learning_block = build_learning_block(learned_examples or [])

    parts = [
        f"ИСТОРИЯ ЧАТА (последние {CONTEXT_WINDOW} сообщений):",
        history_block,
        "",
        f"СОБЕСЕДНИК (имя в telegram сейчас): {current_name}",
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


def reply_instead_of_skip(user_text: str) -> str:
    """When the model returns [SKIP] but the user @mentioned the bot, answer anyway."""
    cleaned = normalize_text(user_text).lower()
    if any(token in cleaned for token in ("трах", "секс", "sex", "еб", "хуй", "бля")):
        return apply_style_rules(
            "фантазии оставь. если хочешь поговорить — задай нормальный вопрос."
        )
    return apply_style_rules(
        "ну такое. я на связи, но давай вопрос по делу."
    )


def contains_search_marker(text: str) -> bool:
    return bool(SEARCH_MARKER_RE.search(text))


def is_nickname_question(text: str) -> bool:
    return bool(NICKNAME_QUESTION_RE.search(normalize_text(text)))


def nickname_reply(current_name: str) -> str:
    return apply_style_rules(f"твой ник в телеге сейчас: {current_name}.")


def refine_search_query(text: str) -> str:
    cleaned = normalize_text(text)
    if not cleaned:
        return cleaned

    patterns = (
        (re.compile(r"^как\s+умер(?:ла)?\s+(.+)$", re.I), r"\1 смерть"),
        (re.compile(r"^когда\s+умер(?:ла)?\s+(.+)$", re.I), r"\1 смерть"),
        (re.compile(r"^кто\s+такой\s+(.+)$", re.I), r"\1"),
        (re.compile(r"^что\s+такое\s+(.+)$", re.I), r"\1"),
    )
    for pattern, replacement in patterns:
        match = pattern.match(cleaned)
        if match:
            return normalize_text(pattern.sub(replacement, cleaned, count=1))
    return cleaned


def guess_search_query_from_user(text: str) -> str | None:
    cleaned = normalize_text(text)
    if not cleaned or is_nickname_question(cleaned):
        return None
    if FACTUAL_USER_QUESTION_RE.search(cleaned):
        return refine_search_query(cleaned)
    return None


def parse_search_request(reply: str) -> str | None:
    raw = reply.strip().strip('"').strip("'")
    if not raw:
        return None

    for pattern in (SEARCH_REQUEST_STRICT_RE, SEARCH_REQUEST_LOOSE_RE):
        match = pattern.match(raw)
        if match:
            query = normalize_text(match.group(1))
            return query or None

    inline = SEARCH_INLINE_RE.search(raw)
    if inline and len(raw) < 160:
        query = normalize_text(inline.group(1))
        return query or None

    return None


def resolve_search_query(model_reply: str, user_text: str) -> str | None:
    query = parse_search_request(model_reply)
    if query:
        return query
    if contains_search_marker(model_reply):
        query = parse_search_request(model_reply.replace(".", ""))
        if query:
            return query
    return guess_search_query_from_user(user_text)


def apply_style_rules(reply: str) -> str:
    if is_skip_reply(reply):
        return reply

    text = FAMILIARITY_RE.sub("", reply)
    text = re.sub(r"[\s,;:]+", " ", text)
    text = normalize_text(text).lower()
    if not text:
        return "ок."

    if text[-1] not in ".!?…":
        text += "."
    return text


def entity_type_name(entity: object) -> str:
    entity_type = getattr(entity, "type", None)
    if entity_type is None:
        return ""
    return str(getattr(entity_type, "value", entity_type)).lower()


async def get_bot_identity(context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str | None]:
    bot_data = context.application.bot_data
    bot_id = bot_data.get(BOT_DATA_ID_KEY)
    bot_username = bot_data.get(BOT_DATA_USERNAME_KEY) or getattr(context.bot, "username", None)

    # Always ensure we have fresh data (important for first messages after restart)
    if bot_id is None or not bot_username:
        try:
            me = await context.bot.get_me()
            bot_id = bot_id or me.id
            bot_username = bot_username or me.username
            bot_data[BOT_DATA_ID_KEY] = bot_id
            bot_data[BOT_DATA_USERNAME_KEY] = _normalize_username(me.username)
        except Exception:
            logger.exception("Failed to refresh bot identity via get_me()")

    return bot_id, _normalize_username(bot_username) if bot_username else None


def register_message_handlers(application: Application) -> None:
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & INCOMING_CONTENT,
            handle_chat_message,
        ),
        group=0,
    )
    application.add_handler(
        MessageHandler(GROUP_INCOMING, handle_chat_message),
        group=0,
    )


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data[BOT_DATA_ID_KEY] = me.id
    application.bot_data[BOT_DATA_USERNAME_KEY] = _normalize_username(me.username)

    if USE_WEBHOOK and WEBHOOK_BASE_URL:
        webhook_url = f"{WEBHOOK_BASE_URL}/{WEBHOOK_PATH}"
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        info = await application.bot.get_webhook_info()
        logger.info(
            "Webhook mode url=%s pending_updates=%s bot=@%s",
            webhook_url,
            info.pending_update_count,
            me.username,
        )
        return

    await application.bot.delete_webhook(drop_pending_updates=True)
    info = await application.bot.get_webhook_info()
    if info.url:
        logger.warning("Cleared stale webhook url=%s before polling", info.url)

    if is_railway_runtime():
        logger.warning(
            "Railway без публичного URL (сервис Unexposed) — остаётся polling и возможен "
            "Conflict при деплое. Settings → Networking → Generate Domain, затем redeploy. "
            "Либо Variables: WEBHOOK_URL=https://ваш-домен.up.railway.app"
        )
        # Старый контейнер при redeploy ещё ~10–20 с держит getUpdates.
        await asyncio.sleep(15)

    logger.info(
        "Polling mode bot id=%s username=@%s pid=%s",
        me.id,
        me.username,
        os.getpid(),
    )


def is_get_updates_conflict(err: BaseException | None) -> bool:
    visited: set[int] = set()
    current = err
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, Conflict):
            return True
        message = str(current)
        if "Conflict" in message and "getUpdates" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


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


def _message_full_text(message: Message) -> str:
    parts: list[str] = []
    if message.text:
        parts.append(message.text)
    if message.caption:
        parts.append(message.caption)
    return "\n".join(parts)


def _message_kind_summary(message: Message) -> str:
    kinds: list[str] = []
    if message.text:
        kinds.append("text")
    if message.caption:
        kinds.append("caption")
    if message.photo:
        kinds.append("photo")
    if message.sticker:
        kinds.append("sticker")
    if message.voice or message.video_note:
        kinds.append("voice")
    if message.video or message.animation:
        kinds.append("video")
    if message.document:
        kinds.append("document")
    if message.new_chat_members:
        kinds.append("new_members")
    if message.left_chat_member:
        kinds.append("left_member")
    return ",".join(kinds) if kinds else "empty"


def _entity_belongs_to_caption(message: Message, entity: object) -> bool:
    if not message.caption:
        return False
    offset = getattr(entity, "offset", None)
    length = getattr(entity, "length", None)
    entity_type = getattr(entity, "type", None)
    for caption_entity in message.caption_entities or ():
        if (
            caption_entity.offset == offset
            and caption_entity.length == length
            and caption_entity.type == entity_type
        ):
            return True
    return False


def _parse_entity_fragment(message: Message, entity: object) -> str | None:
    try:
        if _entity_belongs_to_caption(message, entity):
            return message.parse_caption_entity(entity)
        return message.parse_entity(entity)
    except (RuntimeError, ValueError, IndexError, AttributeError, TypeError):
        return None


def is_reply_to_bot(message: Message, bot_id: int | None, bot_username: str | None) -> bool:
    reply = message.reply_to_message
    if not reply:
        return False

    reply_user = reply.from_user
    if reply_user:
        if bot_id is not None and reply_user.id == bot_id:
            return True

        reply_username = _normalize_username(reply_user.username)
        bot_name = _normalize_username(bot_username)
        if reply_user.is_bot and bot_name and reply_username == bot_name:
            return True

    return False


def is_mention_to_bot(message: Message, bot_id: int | None, bot_username: str | None) -> bool:
    bot_name = _normalize_username(bot_username)
    if not bot_name:
        return False

    # === Robust text-based detection (primary, most reliable in practice) ===
    texts = [
        message.text or "",
        message.caption or "",
        _message_full_text(message) or "",
        get_message_text(message) or "",
    ]
    full_lower = " ".join(t.lower() for t in texts if t)

    # Direct @username mention (most common and reliable)
    if f"@{bot_name}" in full_lower:
        return True

    # Username with @ somewhere near it (catches some weird formatting)
    if "@" in full_lower and bot_name in full_lower:
        return True

    # === Special case for anonymous admins ("Send as Group" / анонимно от лица группы) ===
    # When an admin posts anonymously, Telegram sets sender_chat, and mention entities
    # can be unreliable or missing. In this mode people often still type the bot's name.
    # We become more permissive: if the bot's username appears in the text at all,
    # we treat the message as directed at the bot.
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat and bot_name and bot_name in full_lower:
        return True

    # === Entity-based detection (official Telegram way) ===
    for entity in (message.entities or ()):
        if _entity_targets_bot(message, entity, bot_id, bot_name):
            return True

    for entity in (message.caption_entities or ()):
        if _entity_targets_bot(message, entity, bot_id, bot_name):
            return True

    return False


def _entity_targets_bot(
    message: Message,
    entity: object,
    bot_id: int | None,
    bot_name: str | None,
) -> bool:
    entity_name = entity_type_name(entity)

    if entity_name == MessageEntityType.TEXT_MENTION.value and bot_id is not None:
        mentioned_user = getattr(entity, "user", None)
        return bool(mentioned_user and mentioned_user.id == bot_id)

    if entity_name != MessageEntityType.MENTION.value or not bot_name:
        return False

    fragment = _normalize_username(_parse_entity_fragment(message, entity))
    return fragment == bot_name


def should_reply(message: Message, bot_username: str | None, bot_id: int | None) -> bool:
    chat = getattr(message, "chat", None)
    if not message or not chat:
        return False

    chat_type = getattr(chat, "type", None)
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if is_reply_to_bot(message, bot_id, bot_username):
            return True
        if is_mention_to_bot(message, bot_id, bot_username):
            return True

        # === Anonymous admin ("Send as Group") fallback ===
        # When you (or other admins) post with "Оставаться анонимным", sender_chat is set.
        # The mention detection above now has extra logic for this case.
        # As an ultimate fallback, if the message is from sender_chat and contains the bot name,
        # we treat it as a direct address (very common when admins post as the group).
        sender_chat = getattr(message, "sender_chat", None)
        body = normalize_text(get_message_text(message))
        if sender_chat and bot_username:
            bot_name = _normalize_username(bot_username)
            if bot_name and body and bot_name in body.lower():
                return True

        # Proactive replies in groups: respond to messages strongly related to the vomitboy universe
        # even without explicit @ (e.g. people talking about "ярик", "тошнотики", "immortals", "diet" etc.)
        # This makes the bot feel alive in the chat without violating the "молчи на общий флуд" rule.
        if body and SITE_LORE.is_lore_topic(body):
            return True

        return False

    body = normalize_text(get_message_text(message))
    attachment_present = has_image_attachment(message)
    if chat_type == ChatType.PRIVATE:
        return bool(body or attachment_present)

    return bool(body or attachment_present)


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

    last_exc: Exception | None = None
    for attempt in range(3):  # cheap resilience for transient 5xx / rate limits
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=OPENAI_MODEL,
                    temperature=0.75,
                    max_tokens=400,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                ),
                timeout=OPENAI_TIMEOUT,
            )
            return (response.choices[0].message.content or "").strip()
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning("OpenAI timeout on attempt %s/3", attempt + 1)
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))  # light backoff
            else:
                logger.exception("OpenAI failed after 3 attempts")
    raise last_exc or RuntimeError("OpenAI call failed")


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
    history_snapshot = await safe_snapshot_context(chat_id)
    user_payload = format_user_payload(
        chat_id,
        current_name,
        current_text,
        attachment_note=attachment.note if attachment else None,
        learned_examples=learned_examples,
        history_snapshot=history_snapshot,
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

    if is_nickname_question(current_text):
        return ReplyOutcome(
            text=nickname_reply(current_name),
            learnable=False,
            query=query,
            kind=kind,
        )

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
        logger.info("Model returned SKIP for %r, retrying once", query[:80])
        retry_prompt = (
            f"{user_text}\n\n"
            "Тебя явно позвали в чат. Дай одну короткую реплику в образе ярослава вомитова. "
            "Не используй [SKIP]. На провокации — сухо и с иронией, без морали."
        )
        try:
            reply = await call_openai(retry_prompt)
        except Exception:
            logger.exception("OpenAI SKIP-retry failed")
            return ReplyOutcome(
                text=reply_instead_of_skip(current_text),
                learnable=False,
                query=query,
                kind=kind,
            )
        if is_skip_reply(reply):
            return ReplyOutcome(
                text=reply_instead_of_skip(current_text),
                learnable=False,
                query=query,
                kind=kind,
            )

    search_query = resolve_search_query(reply, current_text)
    if search_query or contains_search_marker(reply):
        if not search_query:
            search_query = refine_search_query(current_text) or "уточни запрос"
        else:
            search_query = refine_search_query(search_query)
        logger.info("Running web search for query=%r", search_query)
        try:
            results = await asyncio.wait_for(search_web(search_query), timeout=25.0)
        except TimeoutError:
            logger.warning("Web search timed out for query=%r", search_query)
            results = []
        search_block = format_search_context(results)
        follow_up = (
            f"{user_payload}\n\n"
            f"{search_block}\n\n"
            f"{REPLY_INSTRUCTIONS}\n"
            "Дай короткий финальный ответ по фактам из поиска. "
            "Не возвращай [SEARCH] и не показывай служебные теги."
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
            logger.info("Model returned SKIP after search for %r", query[:80])
            return ReplyOutcome(
                text=reply_instead_of_skip(current_text),
                learnable=False,
                query=query,
                kind=kind,
            )

    final_reply = apply_style_rules(reply)
    if not final_reply:
        final_reply = reply_instead_of_skip(current_text)
    if contains_search_marker(final_reply):
        final_reply = apply_style_rules(
            "поиск сдох, фактов нет. скажи честно что не нашел, без тегов search."
        )

    return ReplyOutcome(
        text=final_reply,
        learnable=True,
        query=query,
        kind=kind,
    )


async def on_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    bot_id, bot_username = await get_bot_identity(context)
    await message.reply_text(
        apply_style_rules(
            f"жив. id={bot_id} username=@{bot_username or 'нет'}."
        )
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if is_get_updates_conflict(error if isinstance(error, BaseException) else None):
        logger.error(
            "getUpdates conflict (pid=%s): второй poller с тем же TELEGRAM_TOKEN — "
            "старый контейнер Railway при redeploy, второй сервис, или локальный bot.py. "
            "Включи public domain → webhook. Или смени токен в @BotFather. Жду и повторяю…",
            os.getpid(),
        )
        return

    logger.exception("Unhandled bot error", exc_info=error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                apply_style_rules("что то сломалось внутри. глянь логи railway.")
            )
        except Exception:
            logger.exception("Failed to send error reply")


async def on_lore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    name = display_name(update)
    text = message.text.strip() if message.text else "/lore"
    plain_reply, html_reply = build_lore_reply(chat.id)

    await safe_add_message(chat.id, name, text)
    await safe_add_message(chat.id, BOT_DISPLAY_NAME, plain_reply)
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

    try:
        name = display_name(update)
        chat_id = chat.id
        bot_id, bot_username = await get_bot_identity(context)

        # Very loud diagnostic for the most common user complaint ("bot doesn't reply in group")
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            is_reply = is_reply_to_bot(message, bot_id, bot_username)
            is_mention = is_mention_to_bot(message, bot_id, bot_username)

            sender_user = update.effective_user
            sender_chat = getattr(message, "sender_chat", None)  # channel / anonymous admin posts

            is_anonymous = bool(sender_chat)  # "Send as Group" / анонимно от лица группы

            logger.info(
                "GROUP MSG | chat=%s mid=%s | bot=@%s | sender_user_id=%s sender_chat=%s | anonymous=%s | "
                "is_reply=%s is_mention=%s | kind=%s | text=%r",
                chat.id,
                message.message_id,
                bot_username or "?",
                getattr(sender_user, "id", None),
                getattr(sender_chat, "id", None) or getattr(sender_chat, "username", None),
                is_anonymous,
                is_reply,
                is_mention,
                _message_kind_summary(message),
                (normalize_text(get_message_text(message)) or "")[:120],
            )

        reply_needed = should_reply(message, bot_username, bot_id)

        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            logger.info(
                "Group decision chat=%s reply_needed=%s (after should_reply + lore check)",
                chat.id,
                reply_needed,
            )

        raw_text = normalize_text(get_message_text(message))
        full_text = _message_full_text(message)
        attachment = None
        if has_image_attachment(message) and reply_needed:
            attachment = await extract_image_attachment(message)
        if raw_text:
            current_text = cap_user_text(raw_text)   # HARD CAP for safety
        elif attachment or has_image_attachment(message):
            current_text = "[фото]"
        elif message.sticker:
            current_text = "[стикер]"
        elif message.voice or message.video_note:
            current_text = "[голос]"
        elif message.video or message.animation:
            current_text = "[видео]"
        else:
            current_text = ""

        if not current_text and not attachment and not reply_needed:
            return

        # Rate limit expensive LLM calls (protects wallet and prevents self-DoS)
        effective_user_id = update.effective_user.id if update.effective_user else None
        if reply_needed and not await can_reply_now(chat_id, effective_user_id):
            logger.warning(
                "RATE LIMITED chat=%s user_id=%s — this user is hitting the per-user cooldown. "
                "Other people in the group can still mention the bot.",
                chat_id, effective_user_id
            )
            # We still record the user message for context, but skip LLM
            await safe_add_message(
                chat_id,
                name,
                current_text or "[пусто]",
                attachment_note=attachment.note if attachment else None,
            )
            return

        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if not reply_needed:
                if "@" in full_text:
                    logger.warning(
                        "Ignored group message with @ but no bot match chat=%s bot=@%s text=%r",
                        chat_id,
                        bot_username,
                        full_text[:120],
                    )
                else:
                    logger.info(
                        "Ignored group message chat=%s kind=%s text=%r full=%r "
                        "(нужен @%s или реплай на бота)",
                        chat_id,
                        _message_kind_summary(message),
                        (raw_text or "")[:80],
                        full_text[:120],
                        bot_username or "?",
                    )
                    return
            logger.info(
                "Group message chat=%s user=%s mention=%s reply=%s has_photo=%s text=%r",
                chat_id,
                name,
                is_mention_to_bot(message, bot_id, bot_username),
                is_reply_to_bot(message, bot_id, bot_username),
                bool(message.photo),
                (raw_text or "")[:80],
            )

        outcome = ReplyOutcome(
            text=None,
            learnable=False,
            query=build_learning_query(current_text, attachment),
            kind="image" if attachment else "text",
        )
        if reply_needed:
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING,
                )
            except Exception:
                logger.debug("send_chat_action failed", exc_info=True)

            logger.info(
                "Generating reply chat=%s user=%s text=%r",
                chat_id,
                name,
                (current_text or "")[:80],
            )
            try:
                outcome = await asyncio.wait_for(
                    generate_reply(chat_id, name, current_text, attachment=attachment),
                    timeout=REPLY_TIMEOUT,
                )
            except TimeoutError:
                logger.error("generate_reply timed out chat=%s", chat_id)
                outcome = ReplyOutcome(
                    text=apply_style_rules("слишком долго думал. повтори короче."),
                    learnable=False,
                    query=build_learning_query(current_text, attachment),
                    kind="image" if attachment else "text",
                )

        await safe_add_message(
            chat_id,
            name,
            current_text or "[пусто]",
            attachment_note=attachment.note if attachment else None,
        )

        if not outcome.text:
            if reply_needed:
                logger.warning(
                    "Empty reply chat=%s text=%r — sending error fallback",
                    chat_id,
                    (raw_text or "")[:80],
                )
                await message.reply_text(apply_style_rules(FALLBACK_ERROR_REPLY))
            return

        await safe_add_message(chat_id, BOT_DISPLAY_NAME, outcome.text)
        await message.reply_text(outcome.text)

        if outcome.learnable:
            # Offload disk write from event loop (was blocking every reply)
            asyncio.create_task(
                asyncio.to_thread(
                    LEARNING_MEMORY.record, chat_id, outcome.query, outcome.text, kind=outcome.kind
                ),
                name=f"learn-{chat_id}",
            )
    except Exception:
        logger.exception("handle_chat_message failed chat=%s", getattr(chat, "id", None))
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            await message.reply_text(apply_style_rules(FALLBACK_ERROR_REPLY))


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("FATAL: TELEGRAM_TOKEN is required (set in .env or Railway Variables)")

    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is missing — bot will only reply with fallbacks. This is probably not what you want.")

    # Loudly warn about the #1 source of "bot is dead" problems
    transport = "webhook" if USE_WEBHOOK and WEBHOOK_BASE_URL else "polling"
    if transport == "polling" and is_railway_runtime():
        logger.warning(
            "Running in POLLING on Railway without public domain. "
            "You will get Conflict errors on every deploy. "
            "Go to Railway → Settings → Networking → Generate Domain, then set WEBHOOK_URL or redeploy."
        )
    logger.info(
        "Starting bot transport=%s port=%s webhook_base=%s path=%s "
        "(context=%s, model=%s, openai=%s, token=%s, memory=%s, lore=%s)",
        transport,
        HTTP_PORT,
        WEBHOOK_BASE_URL or "(none)",
        WEBHOOK_PATH,
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
    register_message_handlers(app)
    app.add_handler(CommandHandler("lore", on_lore), group=-1)
    app.add_handler(CommandHandler("ping", on_ping), group=-1)
    app.add_error_handler(on_error)

    if USE_WEBHOOK and WEBHOOK_BASE_URL:
        webhook_url = f"{WEBHOOK_BASE_URL}/{WEBHOOK_PATH}"
        app.run_webhook(
            listen="0.0.0.0",
            port=HTTP_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        return

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
