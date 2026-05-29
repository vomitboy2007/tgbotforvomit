"""Build the runtime system prompt from prompt.md and local archives."""

from __future__ import annotations

import re
from pathlib import Path

from corpus import format_corpus_block, load_channel_samples

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "prompt.md"
DEPLOY_BLOCK_RE = re.compile(r"^##\s*Блок\s+6\b.*$", re.IGNORECASE | re.MULTILINE)

RUNTIME_APPENDIX = """
---
Технические правила рантайма:
- Не пересказывай историю чата.
- Не вставляй имя автора перед ответом.
- Если нужно молчать, верни строго [SKIP].
- Пиши только строчными буквами. Каждое законченное предложение заканчивай точкой.
- Не используй «брат», «братан», «бро» и похожую фамильярность.
- Не будь излишне любезным: без восхищения, комплиментов и поддакивания.
- Если сообщение содержит фото, скриншот или картинку, сначала оцени видимые детали и только потом комментируй в живом стиле сообщества.
- Если картинка мутная, обрезанная или детали не читаются, скажи об этом честно и не выдумывай.
- Если в памяти есть похожие удачные ответы, используй их как стилистический ориентир, но не копируй дословно.
- Если не хватает фактов, верни только одну строку: [SEARCH: запрос]. После блока с результатами поиска дай финальный ответ по фактам, без слов про google, поиск и нейросеть.
- Если есть блок ФАКТЫ С VOMITBOY.COM — опирайся на него про сообщество, персонажей /immortals, летопись, diet 13, field log; каждый раз выбирай разные детали, не повторяй один и тот же факт подряд.
- Если человек пишет о реальном риске самоповреждения, не романтизируй и не подыгрывай: коротко попроси написать живому человеку рядом или в экстренную службу.
""".strip()


def load_base_prompt() -> str:
    if not PROMPT_FILE.is_file():
        raise FileNotFoundError(f"Missing {PROMPT_FILE}")

    text = PROMPT_FILE.read_text(encoding="utf-8-sig").strip()
    match = DEPLOY_BLOCK_RE.search(text)
    if match:
        text = text[: match.start()].strip()

    return text


def build_system_prompt() -> str:
    base_prompt = load_base_prompt()
    samples = load_channel_samples()
    corpus_block = format_corpus_block(samples)
    return f"{base_prompt}\n\n{RUNTIME_APPENDIX}{corpus_block}".strip()
