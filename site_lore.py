"""Facts from vomitboycom.neocities.org — structured lore bank for /lore and chat context."""

from __future__ import annotations

import json
import logging
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("vomitbot.lore")

ROOT = Path(__file__).resolve().parent
LORE_DATA_PATH = Path(os.environ.get("SITE_LORE_PATH", str(ROOT / "data" / "site_lore.json")))
LORE_URL = "https://vomitboycom.neocities.org/"
RECENT_LORE_PER_CHAT = 12
LORE_CONTEXT_LIMIT = 5

TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ_]+", re.UNICODE)

LORE_TOPIC_HINTS = (
    "vomitboy",
    "вомит",
    "вомитбой",
    "тошнотик",
    "мутант",
    "ярик",
    "ярослав",
    "вомитов",
    "сайт",
    "neocities",
    "лore",
    "лор",
    "летопись",
    "immortal",
    "мортал",
    "персонаж",
    "diet",
    "диет",
    "field log",
    "нетсталк",
    "маскот",
    "discord",
    "сходка",
    "zigota",
    "кружк",
    "richie",
    "рыгош",
    "vomit gf",
    "горячая штучка",
    "майонез",
)


@dataclass(frozen=True, slots=True)
class LoreEntry:
    id: str
    category: str
    title: str
    text: str
    keywords: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LoreEntry:
        raw_keywords = payload.get("keywords") or []
        keywords = tuple(
            str(item).strip().lower()
            for item in raw_keywords
            if str(item).strip()
        )
        return cls(
            id=str(payload["id"]),
            category=str(payload.get("category", "misc")),
            title=str(payload.get("title", "")),
            text=str(payload["text"]).strip(),
            keywords=keywords,
        )


class SiteLoreBank:
    def __init__(self, path: Path = LORE_DATA_PATH) -> None:
        self.path = path
        self.entries: list[LoreEntry] = []
        self.by_id: dict[str, LoreEntry] = {}
        self.by_category: dict[str, list[LoreEntry]] = {}
        self.recent_by_chat: dict[int, deque[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            logger.warning("Site lore file missing: %s", self.path)
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load site lore from %s", self.path)
            return

        raw_entries = payload.get("entries") or []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            try:
                entry = LoreEntry.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            if not entry.text:
                continue
            self.entries.append(entry)
            self.by_id[entry.id] = entry
            self.by_category.setdefault(entry.category, []).append(entry)

        logger.info(
            "Loaded %s lore entries from %s (%s categories)",
            len(self.entries),
            self.path.name,
            len(self.by_category),
        )

    def _tokenize(self, text: str) -> set[str]:
        return {token for token in TOKEN_RE.findall(text.lower()) if len(token) > 2}

    def _remember(self, chat_id: int, entry_id: str) -> None:
        recent = self.recent_by_chat.setdefault(chat_id, deque(maxlen=RECENT_LORE_PER_CHAT))
        if entry_id in recent:
            recent.remove(entry_id)
        recent.append(entry_id)

    def _pick_pool(
        self,
        pool: list[LoreEntry],
        *,
        chat_id: int | None = None,
        exclude: set[str] | None = None,
    ) -> LoreEntry | None:
        if not pool:
            return None

        blocked = set(exclude or ())
        if chat_id is not None:
            blocked.update(self.recent_by_chat.get(chat_id, ()))

        candidates = [entry for entry in pool if entry.id not in blocked]
        if not candidates:
            candidates = list(pool)

        return random.choice(candidates)

    def pick_random(
        self,
        chat_id: int | None = None,
        *,
        category: str | None = None,
    ) -> LoreEntry | None:
        pool = self.by_category.get(category, []) if category else self.entries
        entry = self._pick_pool(pool, chat_id=chat_id)
        if entry and chat_id is not None:
            self._remember(chat_id, entry.id)
        return entry

    def pick_for_command(self, chat_id: int) -> LoreEntry | None:
        roll = random.random()
        if roll < 0.35:
            category = "character"
        elif roll < 0.55:
            category = "event"
        elif roll < 0.7:
            category = "founder"
        elif roll < 0.82:
            category = random.choice(["culture", "community", "aesthetic"])
        else:
            category = None

        entry = self.pick_random(chat_id, category=category)
        if entry:
            return entry

        return self.pick_random(chat_id)

    def is_lore_topic(self, query: str) -> bool:
        lowered = query.lower()
        if any(hint in lowered for hint in LORE_TOPIC_HINTS):
            return True
        return bool(self.find_relevant(query, limit=1))

    def find_relevant(self, query: str, *, limit: int = LORE_CONTEXT_LIMIT) -> list[LoreEntry]:
        if not self.entries or not query.strip():
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[float, LoreEntry]] = []
        for entry in self.entries:
            score = 0.0
            entry_tokens = self._tokenize(entry.text + " " + entry.title + " " + " ".join(entry.keywords))

            overlap = len(query_tokens & entry_tokens)
            if overlap:
                score += overlap * 1.4

            lowered_query = query.lower()
            for keyword in entry.keywords:
                if keyword in lowered_query:
                    score += 2.5

            for token in query_tokens:
                if token in entry.id.replace("-", " "):
                    score += 1.0

            if entry.category in lowered_query:
                score += 1.2

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def pick_context_entries(self, query: str, chat_id: int | None = None) -> list[LoreEntry]:
        relevant = self.find_relevant(query, limit=LORE_CONTEXT_LIMIT)
        if relevant:
            return relevant

        if not self.is_lore_topic(query):
            return []

        picked: list[LoreEntry] = []
        for category in ("community", "culture", "character", "event"):
            entry = self._pick_pool(
                self.by_category.get(category, []),
                chat_id=chat_id,
                exclude={item.id for item in picked},
            )
            if entry:
                picked.append(entry)
            if len(picked) >= 3:
                break

        if not picked:
            entry = self.pick_random(chat_id)
            if entry:
                picked.append(entry)

        return picked

    def format_context_block(self, entries: list[LoreEntry]) -> str:
        if not entries:
            return ""

        lines = [
            "ФАКТЫ С VOMITBOY.COM (используй по делу, не пересказывай всё подряд, не повторяй одно и то же):",
        ]
        for entry in entries:
            label = entry.title or entry.category
            lines.append(f"- [{entry.category}] {label}: {entry.text}")
        lines.append(f"источник: {LORE_URL}")
        return "\n".join(lines)

    def format_command_reply(self, entry: LoreEntry) -> tuple[str, str]:
        templates = {
            "character": "из списка /immortals на сайте: {text}.",
            "event": "по летописи на сайте: {text}.",
            "founder": "про ярика на сайте: {text}.",
            "culture": "по культуре вомитбоя: {text}.",
            "community": "на сайте написано: {text}.",
            "aesthetic": "по эстетике vomitboy: {text}.",
            "site": "на vomitboy.com: {text}.",
            "media": "в медиаблоке сайта: {text}.",
            "quote": "цитата с сайта: {text}.",
        }
        prefix = templates.get(entry.category, "а ты знал, что {text}.")
        if "{text}" in prefix:
            plain = prefix.format(text=entry.text)
        else:
            plain = f"{prefix} {entry.text}"

        plain = f"{plain} чекни - {LORE_URL}"
        html = (
            f"<b>{escape_html(entry.title or entry.category)}</b>\n"
            f"<blockquote>{escape_html(entry.text)}</blockquote>\n"
            f"чекни - {LORE_URL}"
        )
        return plain, html

    @property
    def size(self) -> int:
        return len(self.entries)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


SITE_LORE = SiteLoreBank()
