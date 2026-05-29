"""Web search for factual answers (DuckDuckGo primary, Google CSE optional fallback)."""

from __future__ import annotations

import asyncio
import logging
import os
from html import unescape
from typing import Any

import httpx

logger = logging.getLogger("vomitbot.search")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "").strip()
SEARCH_MAX_RESULTS = int(os.environ.get("SEARCH_MAX_RESULTS", "5") or "5")


def _clean_snippet(text: str) -> str:
    return unescape(text).replace("\n", " ").strip()


async def _google_custom_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": max(1, min(max_results, 10)),
        "hl": "ru",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

    items = payload.get("items") or []
    results: list[dict[str, str]] = []
    for item in items[:max_results]:
        title = _clean_snippet(str(item.get("title", "")))
        snippet = _clean_snippet(str(item.get("snippet", "")))
        link = _clean_snippet(str(item.get("link", "")))
        if not title and not snippet:
            continue
        results.append({"title": title, "snippet": snippet, "url": link})
    return results


def _duckduckgo_search_sync(query: str, *, max_results: int) -> list[dict[str, str]]:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.warning("duckduckgo_search is not installed")
        return []

    results: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for row in ddgs.text(query, max_results=max_results):
                title = _clean_snippet(str(row.get("title", "")))
                snippet = _clean_snippet(str(row.get("body", "")))
                link = _clean_snippet(str(row.get("href", "")))
                if title or snippet:
                    results.append({"title": title, "snippet": snippet, "url": link})
    except Exception:
        logger.exception("DuckDuckGo search failed for query=%r", query)
    return results


async def _duckduckgo_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    return await asyncio.to_thread(_duckduckgo_search_sync, query, max_results=max_results)


async def search_web(query: str, *, max_results: int | None = None) -> list[dict[str, str]]:
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    limit = max_results or SEARCH_MAX_RESULTS

    results = await _duckduckgo_search(cleaned_query, max_results=limit)
    if results:
        logger.info("DuckDuckGo search returned %s hits for %r", len(results), cleaned_query)
        return results

    logger.warning("DuckDuckGo returned nothing for query=%r", cleaned_query)

    if GOOGLE_API_KEY and GOOGLE_CSE_ID:
        try:
            results = await _google_custom_search(cleaned_query, max_results=limit)
            if results:
                logger.info("Google fallback returned %s hits for %r", len(results), cleaned_query)
                return results
        except Exception:
            logger.exception("Google Custom Search fallback failed for query=%r", cleaned_query)

    logger.warning("No search results for query=%r", cleaned_query)
    return []


def format_search_context(results: list[dict[str, str]]) -> str:
    if not results:
        return "Поиск ничего внятного не дал."

    lines = ["РЕЗУЛЬТАТЫ ПОИСКА (используй как факты, не цитируй дословно источники):"]
    for index, item in enumerate(results, start=1):
        title = item.get("title") or "без названия"
        snippet = item.get("snippet") or ""
        url = item.get("url") or ""
        chunk = f"{index}. {title}"
        if snippet:
            chunk += f" — {snippet}"
        if url:
            chunk += f" ({url})"
        lines.append(chunk)
    return "\n".join(lines)
