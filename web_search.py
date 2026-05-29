"""Web search: DuckDuckGo (retries) + Wikipedia fallback + optional SearXNG."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from html import unescape
from typing import Any
import httpx

logger = logging.getLogger("vomitbot.search")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "").strip()
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
SEARCH_MAX_RESULTS = int(os.environ.get("SEARCH_MAX_RESULTS", "5") or "5")
SEARCH_RETRIES = int(os.environ.get("SEARCH_RETRIES", "3") or "3")


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

    backends = ("auto", "api", "html", "lite")
    last_error: Exception | None = None

    for attempt in range(SEARCH_RETRIES):
        for backend in backends:
            try:
                with DDGS() as ddgs:
                    rows = list(
                        ddgs.text(
                            query,
                            max_results=max_results,
                            region="ru-ru",
                            backend=backend,
                        )
                    )
                results: list[dict[str, str]] = []
                for row in rows:
                    title = _clean_snippet(str(row.get("title", "")))
                    snippet = _clean_snippet(str(row.get("body", "")))
                    link = _clean_snippet(str(row.get("href", "")))
                    if title or snippet:
                        results.append({"title": title, "snippet": snippet, "url": link})
                if results:
                    logger.info(
                        "DuckDuckGo ok backend=%s attempt=%s hits=%s query=%r",
                        backend,
                        attempt + 1,
                        len(results),
                        query,
                    )
                    return results
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "DuckDuckGo backend=%s attempt=%s failed: %s",
                    backend,
                    attempt + 1,
                    exc,
                )
        time.sleep(1.5 * (attempt + 1))

    if last_error:
        logger.exception("DuckDuckGo exhausted for query=%r", query)
    return []


async def _wikipedia_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": max(1, min(max_results, 5)),
        "utf8": 1,
        "srinfo": "totalhits",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://ru.wikipedia.org/w/api.php",
                params=params,
                headers={"User-Agent": "vomitbot/1.0"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.exception("Wikipedia search failed for query=%r", query)
        return []

    hits = payload.get("query", {}).get("search", [])
    results: list[dict[str, str]] = []
    for item in hits[:max_results]:
        title = _clean_snippet(str(item.get("title", "")))
        snippet = _clean_snippet(str(item.get("snippet", "")))
        page_id = item.get("pageid")
        url = f"https://ru.wikipedia.org/?curid={page_id}" if page_id else ""
        if title or snippet:
            results.append({"title": title, "snippet": snippet, "url": url})

    if results:
        logger.info("Wikipedia returned %s hits for %r", len(results), query)
    return results


async def _searxng_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    if not SEARXNG_URL:
        return []

    endpoint = f"{SEARXNG_URL}/search"
    params = {"q": query, "format": "json", "language": "ru-RU"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.exception("SearXNG failed for query=%r", query)
        return []

    results: list[dict[str, str]] = []
    for item in (payload.get("results") or [])[:max_results]:
        title = _clean_snippet(str(item.get("title", "")))
        snippet = _clean_snippet(str(item.get("content", "")))
        link = _clean_snippet(str(item.get("url", "")))
        if title or snippet:
            results.append({"title": title, "snippet": snippet, "url": link})

    if results:
        logger.info("SearXNG returned %s hits for %r", len(results), query)
    return results


async def _duckduckgo_search(query: str, *, max_results: int) -> list[dict[str, str]]:
    return await asyncio.to_thread(_duckduckgo_search_sync, query, max_results=max_results)


def _dedupe_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in results:
        key = (item.get("url") or "") + "|" + (item.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def search_web(query: str, *, max_results: int | None = None) -> list[dict[str, str]]:
    cleaned_query = query.strip().strip(".")
    if not cleaned_query:
        return []

    limit = max_results or SEARCH_MAX_RESULTS
    merged: list[dict[str, str]] = []

    # Wikipedia first: DuckDuckGo often returns 202 on Railway/datacenter IPs.
    merged.extend(await _wikipedia_search(cleaned_query, max_results=limit))

    if len(merged) < limit:
        need = limit - len(merged)
        merged.extend(await _duckduckgo_search(cleaned_query, max_results=need))

    if len(merged) < limit and SEARXNG_URL:
        merged.extend(await _searxng_search(cleaned_query, max_results=limit))

    if not merged and GOOGLE_API_KEY and GOOGLE_CSE_ID:
        try:
            merged.extend(await _google_custom_search(cleaned_query, max_results=limit))
        except Exception:
            logger.exception("Google fallback failed for query=%r", cleaned_query)

    merged = _dedupe_results(merged)[:limit]
    if merged:
        logger.info("search_web total %s hits for %r", len(merged), cleaned_query)
    else:
        logger.warning("search_web found nothing for %r", cleaned_query)
    return merged


def format_search_context(results: list[dict[str, str]]) -> str:
    if not results:
        return (
            "РЕЗУЛЬТАТЫ ПОИСКА: пусто (ни duckduckgo, ни wikipedia ничего не отдали). "
            "Скажи честно, что фактов не нашел, без выдумок."
        )

    lines = ["РЕЗУЛЬТАТЫ ПОИСКА (опирайся на это, не показывай пользователю теги search):"]
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
