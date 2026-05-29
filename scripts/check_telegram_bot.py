"""Check who owns the bot token (webhook vs polling). Does not call getUpdates."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    print("TELEGRAM_TOKEN не задан (.env или переменная окружения).")
    sys.exit(1)


def api(method: str) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    me = api("getMe")
    wh = api("getWebhookInfo")

    print("=== getMe ===")
    print(json.dumps(me, ensure_ascii=False, indent=2))

    print("\n=== getWebhookInfo ===")
    print(json.dumps(wh, ensure_ascii=False, indent=2))

    webhook_url = (wh.get("result") or {}).get("url") or ""
    print("\n=== Вывод ===")
    if webhook_url:
        print(f"Сейчас активен WEBHOOK: {webhook_url}")
        print("Polling (Railway/local) будет конфликтовать, пока webhook не снят.")
    else:
        print("Webhook не установлен — кто-то использует long polling (getUpdates).")
        print("Допустим только ОДИН процесс polling с этим токеном.")

    print("\nRailway env (если есть):")
    for key in (
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PUBLIC_DOMAIN",
        "RAILWAY_STATIC_URL",
        "WEBHOOK_URL",
    ):
        value = os.environ.get(key, "")
        if value:
            print(f"  {key}={value}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f"Telegram API error: {exc}")
        sys.exit(1)
