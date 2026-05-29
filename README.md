# VOMITBOY Telegram Bot

Бот отвечает в чатах от лица **Ярослава Вомитова** (персона VOMITBOY.COM). Стиль и правила — в `prompt.md`, примеры постов подтягиваются из `messages.html`.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# заполните .env реальными ключами
python bot.py
```

## Поведение

- В **личке** отвечает на любой текст.
- В **группах** — на реплай боту или `@username` бота.
- Контекст: скользящее окно `CONTEXT_WINDOW` сообщений на `chat_id` (в RAM, сбрасывается при рестарте).
- На троллинг модель может вернуть `[SKIP]` — бот молчит.

## Деплой на Railway

1. Запушьте репозиторий на GitHub.
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
3. В **Variables** задайте `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, `CONTEXT_WINDOW` (опционально `OPENAI_MODEL`).
4. Railway подхватит `Procfile` (`worker: python bot.py`).

Каждый push в основную ветку — автоматический редеплой.

## Файлы

| Файл | Назначение |
|------|------------|
| `bot.py` | Long polling, OpenAI, контекст |
| `prompt_loader.py` | Системный промпт из `prompt.md` |
| `corpus.py` | Сэмплы из `messages.html` |
| `prompt.md` | Персона и правила (блок 6 — только деплой) |

**Не коммитьте `.env`** — секреты только в Railway Variables.
