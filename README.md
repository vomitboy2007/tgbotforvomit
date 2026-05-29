# VOMITBOY Telegram Bot

Telegram-бот, который отвечает в личке и групповых чатах по правилам из `prompt.md`.
Системный промпт собирается из `prompt.md`, а примеры живого тона подтягиваются из локального экспорта `messages.html` или `index/vn-game/yaroslav/messages`.

## Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Перед запуском заполните `.env` реальными значениями:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
OPENAI_API_KEY=your_openai_api_key
CONTEXT_WINDOW=15
OPENAI_MODEL=gpt-4o-mini
```

## Поведение

- В личных сообщениях бот отвечает на любой текст.
- В группах отвечает только на реплай боту или упоминание `@username` бота.
- История хранится в памяти процесса как скользящее окно `chat_id -> deque`.
- При рестарте Railway память сбрасывается. Для долгой памяти нужен Redis или PostgreSQL.
- Если модель возвращает `[SKIP]`, бот ничего не отправляет.

## Деплой на Railway

1. Запушьте репозиторий на GitHub.
2. В Railway создайте `New Project -> Deploy from GitHub repo`.
3. В `Variables` добавьте `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, `CONTEXT_WINDOW` и при необходимости `OPENAI_MODEL`.
4. Railway подхватит `Procfile` и запустит worker:

```procfile
worker: python bot.py
```

## Файлы

| Файл | Назначение |
| --- | --- |
| `bot.py` | Long polling, OpenAI, Telegram handlers, память чата |
| `prompt_loader.py` | Сборка runtime-промпта из `prompt.md` |
| `corpus.py` | Извлечение примеров из Telegram HTML/TXT экспорта |
| `prompt.md` | Персона, стиль, правила ответа и блок деплоя |
| `Procfile` | Точка входа Railway worker |
| `.env.example` | Шаблон переменных окружения без секретов |

Не коммитьте `.env`: реальные ключи должны жить локально или в Railway Variables.
