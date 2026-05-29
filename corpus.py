"""Extract sample posts from Telegram HTML exports for the system prompt."""

from __future__ import annotations

import html
import re
from pathlib import Path

TEXT_BLOCK_RE = re.compile(
    r'<div class="text">\s*(.*?)\s*</div>',
    re.DOTALL | re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def _clean_fragment(raw: str) -> str:
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def load_channel_samples(
    path: Path | None = None,
    *,
    max_samples: int = 35,
    min_len: int = 8,
    max_len: int = 280,
) -> list[str]:
    root = Path(__file__).resolve().parent
    export_path = path or root / "messages.html"
    if not export_path.is_file():
        return []

    content = export_path.read_text(encoding="utf-8", errors="ignore")
    seen: set[str] = set()
    samples: list[str] = []

    for match in TEXT_BLOCK_RE.finditer(content):
        cleaned = _clean_fragment(match.group(1))
        if not cleaned or cleaned in seen:
            continue
        if len(cleaned) < min_len or len(cleaned) > max_len:
            continue
        if cleaned.startswith("http"):
            continue
        seen.add(cleaned)
        samples.append(cleaned)
        if len(samples) >= max_samples:
            break

    return samples


def format_corpus_block(samples: list[str]) -> str:
    if not samples:
        return ""
    lines = "\n".join(f"- {s}" for s in samples)
    return (
        "\n\n---\nПримеры реальных постов канала (тон и лексика):\n"
        f"{lines}\n"
    )
