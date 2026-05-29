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

В логах `Polling mode` на Railway почти всегда значит: сервис **Unexposed** (нет домена) → webhook не включается → при каждом redeploy **старый и новый контейнер** на 10–30 с оба делают `getUpdates`.

#### Как починить (рекомендуется)

1. Railway → сервис `tgbotforvomit` → **Settings → Networking → Generate Domain** (перестанет быть Unexposed).
2. **Redeploy**. В логах должно быть: `Webhook mode url=https://....up.railway.app/...`
3. **Replicas = 1** (у тебя уже так на скрине).

Либо в **Variables** вручную: `WEBHOOK_URL=https://твой-домен.up.railway.app` и redeploy.

#### Как проверить, что токен не дублируется

Скрипт **не** запускает бота, только смотрит статус в Telegram:

```powershell
$env:TELEGRAM_TOKEN="токен_из_railway_variables"
python scripts/check_telegram_bot.py
```

- `url` пустой → кто-то в polling (Railway, локально или второй сервис).
- `url` есть → webhook; polling с тем же токеном даст Conflict.

#### Где искать «второй процесс», если локально не запускал

| Место | Что проверить |
| --- | --- |
| Railway | В проекте **один** сервис с `TELEGRAM_TOKEN`, Replicas = 1, нет второго деплоя/форка |
| Старый контейнер | Сразу после Redeploy 15–30 с — нормальный Conflict, должен пройти |
| Другой хостинг | Старый VPS/Heroku/Render с тем же токеном |
| Токен утёк | @BotFather → **Revoke** / новый токен → обновить Variable → redeploy (убивает всех pollers) |

Локальный `python bot.py` и Railway с **одним** токеном одновременно — тоже Conflict.

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
