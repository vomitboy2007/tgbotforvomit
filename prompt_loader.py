"""Build the author system prompt from prompt.md and local archives."""

from __future__ import annotations

from pathlib import Path

from corpus import format_corpus_block, load_channel_samples

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "prompt.md"
BLOCK6_MARKER = "## Блок 6"

AUTHOR_BIO = """
Кратко о себе (из автобиографии на сайте):
Ярик, основатель vomitboy.com с 2021. Бывший админ пабликов, сейчас врач.
Темы: подпольная эстетика, двач-архивы, вайбкодинг, одиночество, инцельская ирония, еда с помойки/светофора, медицинский бэкграунд как контраст образу.
""".strip()


def load_base_prompt() -> str:
    if not PROMPT_FILE.is_file():
        raise FileNotFoundError(f"Missing {PROMPT_FILE}")

    text = PROMPT_FILE.read_text(encoding="utf-8")
    if BLOCK6_MARKER in text:
        text = text.split(BLOCK6_MARKER, 1)[0].strip()

    # Drop the title line if it's only a role description header.
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("Ты —"):
        text = "\n".join(lines[1:]).strip()

    return text


def build_system_prompt() -> str:
    base = load_base_prompt()
    samples = load_channel_samples()
    corpus = format_corpus_block(samples)
    return f"{base}\n\n{AUTHOR_BIO}{corpus}"
