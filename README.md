# VOMITBOY Telegram Bot

Telegram-бот, который отвечает в личке и групповых чатах по правилам из `prompt.md`.
Системный промпт собирается из `prompt.md`, а примеры живого тона подтягиваются из локального экспорта `messages.html` или `index/vn-game/yaroslav/messages`.
Бот умеет разбирать фото и скриншоты, а похожие удачные ответы подмешивает из локальной памяти, чтобы держать тон ближе к сообществу.

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
SEARCH_MAX_RESULTS=5
```

Поиск фактов: **DuckDuckGo** (работает без ключей). `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` — только опциональный fallback, если DDG пустой.

## Поведение

- В личных сообщениях бот отвечает на любой текст.
- В группах отвечает на **реплай** боту или **@username** бота (фильтр `filters.Mention` + проверка id).
- В @BotFather: **Group Privacy → Turn off** не обязательно, но mention должен быть именно на @username вашего бота.
- Пишет строчными буквами, предложения заканчивает точкой, без «брат/братан» и без лишней любезности.
- Если не знает фактов — ищет через DuckDuckGo и отвечает по результатам.
- Фото и скриншоты бот тоже умеет анализировать, если на сообщение есть право ответа по тем же правилам.
- Команда `/lore` выдаёт случайный факт из `data/site_lore.json` (персонажи, летопись, культура, ярик) — без повторов подряд в одном чате.
- На вопросы про vomitboy/персонажей бот подмешивает релевантные факты с [vomitboycom.neocities.org](https://vomitboycom.neocities.org/).
- История хранится в памяти процесса как скользящее окно `chat_id -> deque`.
- Удачные пары вопрос-ответ дополнительно складываются в `runtime/learned_turns.jsonl` и используются как похожие примеры при следующих ответах.
- При рестарте Railway RAM-память всё равно сбрасывается. Для действительно долгой памяти нужен Redis, PostgreSQL или отдельный volume.
- Если модель возвращает `[SKIP]`, бот ничего не отправляет.

## Деплой на Railway

1. Запушьте репозиторий на GitHub.
2. В Railway создайте `New Project -> Deploy from GitHub repo`.
3. В `Variables` добавьте `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, `CONTEXT_WINDOW`, при необходимости `OPENAI_MODEL` и `SEARCH_MAX_RESULTS`.
4. В @BotFather для групп: **Bot Settings → Group Privacy → Turn off**, если хотите, чтобы бот видел все сообщения. С включённой privacy бот всё равно получает reply и mention.
5. Railway подхватит `Procfile` и запустит worker:

```procfile
web: python bot.py
```

6. В сервисе Railway включите **Public Networking** (нужен URL для webhook).
7. **Replicas = 1**. На Railway бот сам переходит в **webhook** (по `RAILWAY_PUBLIC_DOMAIN`), без `getUpdates` — так нет Conflict при деплое.

Локально без публичного URL — обычный **polling** (`python bot.py`). Не запускайте локально и Railway одновременно с одним токеном.

### Ошибка `Conflict: terminated by other getUpdates request`

Два процесса одновременно делали **polling** (`getUpdates`) с одним токеном.

1. Останови **локальный** `python bot.py`.
2. Railway → **Replicas = 1**, удали дублирующий сервис с тем же токеном.
3. После redeploy в логах должно быть `Webhook mode url=https://...` (не polling).
4. Включи **Public Networking** у сервиса, иначе webhook не дойдёт до бота.
5. Не держи два деплоя (старый + новый) с одним токеном дольше пары минут.

## Файлы

| Файл | Назначение |
| --- | --- |
| `bot.py` | Long polling, OpenAI, Telegram handlers, память чата и локальное обучение |
| `prompt_loader.py` | Сборка runtime-промпта из `prompt.md` |
| `corpus.py` | Извлечение примеров из Telegram HTML/TXT экспорта |
| `web_search.py` | DuckDuckGo search (+ optional Google fallback) |
| `site_lore.py` + `data/site_lore.json` | База фактов с сайта (55+ записей) |
| `scripts/build_site_lore.py` | Обновить персонажей/летопись из `index.html` |
| `prompt.md` | Персона, стиль, правила ответа и блок деплоя |
| `Procfile` | Точка входа Railway worker |
| `.env.example` | Шаблон переменных окружения без секретов |

Не коммитьте `.env`: реальные ключи должны жить локально или в Railway Variables.
