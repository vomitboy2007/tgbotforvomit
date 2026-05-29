"""Load representative messages from local Telegram exports."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

WHITESPACE_RE = re.compile(r"\s+")
TEXT_EXPORT_EXTENSIONS = {".txt", ".md", ""}
HTML_EXPORT_EXTENSIONS = {".html", ".htm"}
EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\u2600-\u27bf"
    "]"
)


class TelegramTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[str] = []
        self._capture_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_depth:
            if tag == "br":
                self._parts.append("\n")
                return
            self._capture_depth += 1
            return

        if tag != "div":
            return

        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        if "text" in classes and "bold" not in classes:
            self._capture_depth = 1
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self._capture_depth:
            return

        self._capture_depth -= 1
        if self._capture_depth == 0:
            text = clean_text("".join(self._parts))
            if text:
                self.messages.append(text)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_depth:
            self._parts.append(data)


def clean_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def candidate_export_paths(root: Path) -> list[Path]:
    configured = root / "index" / "vn-game" / "yaroslav" / "messages"
    candidates: list[Path] = []

    if configured.is_file():
        candidates.append(configured)
    if configured.with_suffix(".html").is_file():
        candidates.append(configured.with_suffix(".html"))
    if configured.is_dir():
        candidates.extend(sorted(configured.glob("*.html")))
        candidates.extend(sorted(configured.glob("*.txt")))

    fallback = root / "messages.html"
    if fallback.is_file():
        candidates.append(fallback)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)

    return unique


def extract_messages(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    suffix = path.suffix.lower()

    if suffix in HTML_EXPORT_EXTENSIONS or "<div" in content:
        parser = TelegramTextParser()
        parser.feed(content)
        return parser.messages

    if suffix in TEXT_EXPORT_EXTENSIONS:
        return [clean_text(line) for line in content.splitlines()]

    return []


def iter_export_messages(paths: Iterable[Path]) -> Iterable[str]:
    for path in paths:
        if not path.is_file():
            continue
        yield from extract_messages(path)


def load_channel_samples(
    path: Path | None = None,
    *,
    max_samples: int = 35,
    min_len: int = 8,
    max_len: int = 280,
) -> list[str]:
    root = Path(__file__).resolve().parent
    paths = [path] if path else candidate_export_paths(root)

    seen: set[str] = set()
    samples: list[str] = []

    for message in iter_export_messages(paths):
        cleaned = clean_text(message)
        if not cleaned or cleaned in seen:
            continue
        if len(cleaned) < min_len or len(cleaned) > max_len:
            continue
        if cleaned.startswith(("http://", "https://")):
            continue
        if EMOJI_RE.search(cleaned):
            continue

        seen.add(cleaned)
        samples.append(cleaned)
        if len(samples) >= max_samples:
            break

    if not samples:
        # This is important for persona fidelity — the bot will be more "generic" without examples
        import logging
        logging.getLogger("vomitbot").warning(
            "No channel samples loaded for style examples (corpus empty). "
            "Check messages.html or index/vn-game/yaroslav/messages. Bot will still work but tone will drift."
        )
    return samples


def format_corpus_block(samples: list[str]) -> str:
    if not samples:
        return ""

    lines = "\n".join(f"- {sample}" for sample in samples)
    return (
        "\n\n---\n"
        "Примеры реальных сообщений канала для тона и лексики:\n"
        f"{lines}\n"
    )
