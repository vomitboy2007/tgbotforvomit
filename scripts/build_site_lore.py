"""Regenerate data/site_lore.json immortals and chronicle from index.html (optional maintenance)."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
OUT = ROOT / "data" / "site_lore.json"

IMMORTAL_RE = re.compile(
    r'<span class="iconic-name">([^<]+)</span><span class="iconic-tag">([^<]+)</span>',
    re.IGNORECASE,
)
CHRONICLE_RE = re.compile(
    r"<strong>([^<]+)</strong>\s*([^<]+?)\s*<br>",
    re.IGNORECASE,
)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", value.strip().lower())
    return cleaned.strip("-") or "entry"


def main() -> None:
    if not INDEX.is_file():
        raise SystemExit(f"Missing {INDEX}")

    html = INDEX.read_text(encoding="utf-8")
    payload = json.loads(OUT.read_text(encoding="utf-8"))
    entries = [item for item in payload.get("entries", []) if item.get("category") not in {"character", "event"}]

    for index, (name, tag) in enumerate(IMMORTAL_RE.findall(html), start=1):
        name = unescape(name.strip())
        tag = unescape(tag.strip())
        entries.append(
            {
                "id": f"char-{slugify(name)}",
                "category": "character",
                "title": name,
                "text": f"{name} — immortals #{index:02d}, тег {tag}.",
                "keywords": [name.lower(), tag.lower(), "immortals", f"{index:02d}"],
            }
        )

    manifesto = html.split("id=\"manifesto\"", 1)[-1][:2500]
    for date, description in CHRONICLE_RE.findall(manifesto):
        date = unescape(date.strip())
        description = unescape(description.strip())
        entries.append(
            {
                "id": f"event-{slugify(date)}",
                "category": "event",
                "title": date,
                "text": f"{date} — {description}.",
                "keywords": [date.lower(), "летопись", "event"],
            }
        )

    payload["entries"] = entries
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} entries to {OUT}")


if __name__ == "__main__":
    main()
