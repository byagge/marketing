# Autoposter HTTP API

Управление sender с другого бота / агента / скрипта. Telegram-бот **не меняется**: те же кнопки, те же SQLite, те же циклы рассылки. API крутится **в том же процессе**, что и бот (иначе `start/stop` рассылки и Pyrogram-клиенты недоступны).

Интерактивная схема: после старта бота открой [http://127.0.0.1:8787/docs](http://127.0.0.1:8787/docs) (`/openapi.json` — для LLM-агента).

---

## 1. Запуск и доступ

API поднимается автоматически вместе с ботом (`main.py`). Если порта нет, пакета нет или `API_ENABLED=False` — бот всё равно работает как раньше.

```python
# config.py
API_ENABLED = True
API_HOST = "127.0.0.1"   # не выставляй 0.0.0.0 без файрвола
API_PORT = 8787
API_KEY = ""             # если пусто — ключ пишется в api_key.txt
```

Зависимости: `fastapi`, `uvicorn`, `python-multipart` (уже в `requirements.txt`).

**Базовый URL:** `http://127.0.0.1:8787`

### Авторизация

Все пути `/v1/*` требуют ключ. `/health` — без ключа (проверка, что процесс жив).

| Заголовок | Пример |
|---|---|
| `X-API-Key` | `X-API-Key: <ключ>` |
| `Authorization` | `Authorization: Bearer <ключ>` |
| `Authorization` без Bearer | `Authorization: <ключ>` |

Если переданы оба — берётся `X-API-Key`. Неверный или пустой ключ → `401 {"detail":"invalid api key"}`.

---

## 2. Как устроены аккаунты

Каждый сендер — это запись в `master.db` + папка `accounts/{account_id}/` (`.session`, `database.db`, медиа).

**Все настройки конкретного аккаунта — только через `{account_id}` в URL.**  
`POST /v1/accounts/{id}/select` влияет лишь на то, какой аккаунт открыт в **мастер-боте Telegram**. API от выбранного аккаунта не зависит.

Типичный цикл агента:

1. `POST /v1/accounts` → в ответе `account_id`
2. `PUT /v1/accounts/{id}/post` — текст/фото
3. `PATCH /v1/accounts/{id}/chats/{chat_id}` — чаты
4. `PUT /v1/accounts/{id}/intervals/...` — паузы
5. `PUT /v1/accounts/{id}/cloak` и `/mentions`
6. `POST /v1/accounts/{id}/spam/start`

### Что можно без live-клиента

Чтение/запись SQLite (пост, интервалы, клоакинг-текст, флаги чатов) работают и если процесс аккаунта остановлен.

### Что требует запущенный аккаунт (`live: true`)

Иначе `409` «Сначала POST /v1/accounts/{id}/start»:

- старт рассылки
- живой список чатов (диалоги Telegram)
- резолв ссылки `t.me/...`
- выход из чата
- исключения клоакинга/автоответа по `@username` (по числовому `user_id` — нет)
- участники группы → исключения клоакинга
- добавление аккаунта (сразу бутит клиент)

`GET /v1/accounts/{id}` → поле `live`.

---

## 3. Коды ошибок

| HTTP | Когда |
|---|---|
| 401 | нет/неверный API-ключ |
| 404 | неизвестный `account_id` |
| 409 | аккаунт не запущен / рассылка глобально выключена / пул прокси ещё не поднят |
| 400 | невалидные данные (токен, `.session`, интервал, ссылка, mode) |
| 422 | тело не прошло схему FastAPI (нет обязательного поля) |

Тело ошибки: `{"detail": "текст"}`.

---

## 4. Аккаунты

### `GET /v1/accounts`

Список. Токен бота **не отдаётся**, только `bot_token_hint` вида `123456:…abcd`.

```json
{
  "accounts": [
    {
      "account_id": "a1b2c3d4e5f6",
      "label": "main",
      "username": "myuser",
      "user_id": 123456789,
      "phone": "",
      "status": "running",
      "enabled": true,
      "live": true,
      "spam": false,
      "spam_task": false,
      "selected": true,
      "bot_token_hint": "123456:…wxyz",
      "last_chat_id": "",
      "last_chat_title": "",
      "last_sent_at": 0
    }
  ]
}
```

### `POST /v1/accounts` (multipart)

Как в боте: файл `.session` + bot token. Аккаунт **сразу запускается**.

| Поле | Тип | |
|---|---|---|
| `session` | файл | обязателен, ≥ 100 байт |
| `bot_token` | form | `123456:AAH...` |
| `label` | form | необязательно; иначе имя файла |

```bash
curl -s -X POST http://127.0.0.1:8787/v1/accounts \
  -H "X-API-Key: KEY" \
  -F "bot_token=123456:AAH...." \
  -F "label=acc1" \
  -F "session=@./acc1.session"
```

Ответ:

```json
{
  "ok": true,
  "account_id": "a1b2c3d4e5f6",
  "username": "myuser",
  "user_id": 123456789,
  "label": "acc1",
  "live": true
}
```

Дальше везде этот `account_id`. Невалидный token / битая session → `400`.

### `POST /v1/accounts/json`

То же, без multipart:

```json
{
  "bot_token": "123456:AAH....",
  "session_base64": "<base64 файла .session>",
  "label": "acc1",
  "session_name": "acc1.session"
}
```

### `GET /v1/accounts/{id}` и `GET /v1/accounts/{id}/status`

Полный снимок: аккаунт + пост + интервалы + клоакинг + автоответ + `global_spam_disabled`.

### `POST /v1/accounts/{id}/select`

Выбрать аккаунт в мастер-боте Telegram. На API-роуты не влияет.

### `POST /v1/accounts/{id}/start` / `…/stop`

Поднять / остановить Pyrogram + polling бота этого аккаунта. `stop` не удаляет файлы.

### `DELETE /v1/accounts/{id}?remove_files=true`

Остановить и удалить из `master.db`. `remove_files=false` оставляет папку на диске.

---

## 5. Рассылка

Совпадает с кнопками «Старт / Стоп» в боте.

### `POST /v1/accounts/{id}/spam/start`

- Нужен `live` аккаунт.
- Если в «⚙️ Общие» включён глобальный стоп — `409`.
- Как в боте: ставит `SPAM=1` и **включает `spam_enabled` у всех чатов** (индивидуальный «стоп в чате» сбрасывается). Чаты с `active: false` по-прежнему не получают посты.

```json
{ "ok": true, "account_id": "…", "spam": true, "already_running": false }
```

### `POST /v1/accounts/{id}/spam/stop`

Остановить цикл этого аккаунта.

### `POST /v1/spam/stop-all`

Стоп у всех запущенных аккаунтов. То же, что глобальный выключатель, но **без** установки флага `spam_disabled` (после этого можно снова стартовать).

---

## 6. Пост аккаунта

Эквивалент меню «📮 Пост».

### `GET /v1/accounts/{id}/post`

```json
{
  "mode": "post",
  "text": "Текст",
  "photo": "accounts/…/media/….jpg",
  "link": "",
  "link_src_chat_id": null,
  "link_src_msg_id": null,
  "template_chat_id": "",
  "template_message_id": null,
  "has_photo": true
}
```

`mode`: `post` | `link`.

### `PUT /v1/accounts/{id}/post`

Частичное обновление: передавай только нужные поля.

```json
{
  "mode": "post",
  "text": "Новый текст",
  "link": "https://t.me/channel/123",
  "link_chat_id": -100123,
  "link_msg_id": 45,
  "clear_link": false,
  "clear_photo": false,
  "clear_template": false,
  "photo_path": "C:/media/post.jpg",
  "photo_base64": "<optional>",
  "photo_filename": "post.jpg"
}
```

| Поле | Смысл |
|---|---|
| `text` | текст поста (entities/premium emoji через API **не сохраняются** — только plain text) |
| `mode` | `post` или `link` |
| `link` | URL `t.me/...`; резолвится через клиент аккаунта (нужен live) |
| `link_chat_id` + `link_msg_id` | задать источник ссылки без резолва URL |
| `photo_base64` / `photo_path` | сохранить медиа в `accounts/{id}/media/` |
| `clear_photo` / `clear_link` / `clear_template` | сброс |

Шаблон «переслать сообщение как есть» в боте задаётся **пересылкой в Telegram**. Через API шаблон можно только сбросить (`clear_template`), не создать.

### `POST /v1/accounts/{id}/post/photo`

`multipart`: поле `file` (jpg/png/mp4/…).

### `DELETE /v1/accounts/{id}/post/photo`

Сброс медиа.

```bash
curl -s -X PUT http://127.0.0.1:8787/v1/accounts/ID/post \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d "{\"mode\":\"post\",\"text\":\"Привет\"}"
```

---

## 7. Интервалы (каждый пункт отдельно)

Как меню «⏱ Интервал». Все значения — секунды; вместо числа можно `"1h"`, `"30m"`, `"1h30m"`. Если `max_sec` нет — равен `min_sec`.

### `GET /v1/accounts/{id}/intervals`

```json
{
  "between": { "min_sec": 60, "max_sec": 120 },
  "cycle": { "min_sec": 300, "max_sec": 600 },
  "per_chat": { "min_sec": 60, "max_sec": 120 },
  "parallel_sends": 4,
  "send_paid": false
}
```

| Ключ | Как в боте |
|---|---|
| `between` | пауза **между разными чатами** |
| `cycle` | пауза **между циклами** (когда все активные чаты получили пост) |
| `per_chat` | пауза **до следующего поста в тот же чат** |
| `parallel_sends` | сколько чатов слать одновременно (≥ 1) |
| `send_paid` | слать в платные каналы |

```http
PUT /v1/accounts/{id}/intervals/between     {"min_sec": 30, "max_sec": 90}
PUT /v1/accounts/{id}/intervals/cycle       {"min_sec": "5m", "max_sec": "10m"}
PUT /v1/accounts/{id}/intervals/per-chat    {"min_sec": 3600}
PUT /v1/accounts/{id}/intervals/parallel    {"value": 4}
PUT /v1/accounts/{id}/intervals/paid        {"enabled": false}
```

Ответ каждого PUT — полный объект интервалов, как у GET.

---

## 8. Упоминания (глобально по аккаунту)

Меню «👁 Упоминания».

```http
GET /v1/accounts/{id}/mentions
PUT /v1/accounts/{id}/mentions
{"enabled": true}
```

Пер-чат переопределение — в PATCH чата, поле `mention`.

---

## 9. Чаты

Меню «💬 Мои чаты» → настройки одного чата.

`chat_id` — строка, обычно `-100…` (супергруппа) или `-…`. В URL минус допустим: `/chats/-100123456`.

### `GET /v1/accounts/{id}/chats`

Склеивает живые диалоги аккаунта (если live) и уже сохранённые в БД. Если клиент недоступен — `live_error` с текстом, список из БД всё равно вернётся.

```json
{
  "chats": [ { "chat_id": "-1001", "title": "Chat A", "active": true, "...": "..." } ],
  "count": 1,
  "live_error": null
}
```

### `GET /v1/accounts/{id}/chats/{chat_id}`

Если чата не было в БД — создаётся пустая запись (как «открыть настройки» в боте).

### Объект чата

| Поле | Смысл | Как в боте |
|---|---|---|
| `active` | участвует в рассылке | «Включить / выключить из рассылки» |
| `spam_enabled` | флаг «стоп в чате» | сбрасывается в `true` при **старте рассылки** |
| `mention` | `true` / `false` / `"global"` | цикл кнопки упоминаний чата |
| `mode` | `"global"` / `"post"` / `"link"` | общий / свой пост / своя ссылка |
| `effective_mode` | что реально уйдёт | с учётом глобального поста |
| `text` | свой текст чата | «📜 Текст чата» |
| `additional_text` | доп. текст к сообщению | «🗃 Доп. текст» |
| `photo` | путь к своему медиа | «🏙 Фото чата» |
| `interval_min_sec` / `max_sec` | свой интервал; `null` = как глобально | «🕒 Свой интервал» |
| `link_src_chat_id` / `link_src_msg_id` | источник режима link | |
| `last_sent_at` | unix time последней отправки | |

### `PATCH /v1/accounts/{id}/chats/{chat_id}`

Только переданные поля. Не смешивай в одном запросе взаимоисключающие `mode` + `text` + `link`: обработка по порядку **mode → text → link**, последнее контент-поле побеждает (`link` перекрывает `text`).

```json
{
  "active": true,
  "spam_enabled": true,
  "mention": "global",
  "mode": "post",
  "text": "Свой пост для этого чата",
  "additional_text": "",
  "interval": "1h",
  "interval_min_sec": 50,
  "interval_max_sec": 70,
  "clear_interval": false,
  "clear_photo": false,
  "link": "https://t.me/c/123/45",
  "photo_base64": null,
  "photo_path": null
}
```

| Поле | |
|---|---|
| `active: false` | чат не получает рассылку (💤) |
| `spam_enabled: false` | «🛑 Стоп в чате» до следующего spam/start |
| `mention` | `true`, `false`, `"global"` (не `null`) |
| `mode: "global"` | сброс своего поста/ссылки/фото чата |
| `interval` | одно число/`1h` → min=max (как в боте, без рандома) |
| `interval_min_sec` + `max_sec` | диапазон |
| `clear_interval` | снова «как глобально» |

```bash
curl -s -X PATCH http://127.0.0.1:8787/v1/accounts/ID/chats/-100123 \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d "{\"active\":true,\"text\":\"оффер\",\"interval\":\"2h\"}"
```

### `POST /v1/accounts/{id}/chats/{chat_id}/photo`

Multipart `file`.

### `POST /v1/accounts/{id}/chats/{chat_id}/reset`

Как «🗑 Сброс настроек чата»: свой пост/ссылка/фото/интервал. **Не** трогает `active`, `spam_enabled`, `mention`, доп. текст.

### `POST /v1/accounts/{id}/chats/{chat_id}/leave`

Выйти из чата в Telegram. Нужен live. Неудача → `400`.

---

## 10. Клоакинг

Меню «🛡 Клоакинг». Настройки **на каждый аккаунт** (своя БД).

### `GET /v1/accounts/{id}/cloak`

```json
{
  "enabled": true,
  "text": "текст ответа",
  "exceptions": [{ "user_id": 1, "label": "u" }],
  "admins_seen": [
    { "user_id": 2, "label": "Admin", "chat_id": "-100", "chat_title": "G", "last_seen_at": 0 }
  ]
}
```

### `PUT /v1/accounts/{id}/cloak`

```json
{ "enabled": true, "text": "Ответ не-админам" }
```

Entities/premium emoji через API не пишутся.

### Исключения

```http
POST /v1/accounts/{id}/cloak/exceptions
{"user_id": 111} 
{"username": "durov", "label": "опционально"}

DELETE /v1/accounts/{id}/cloak/exceptions/111

POST /v1/accounts/{id}/cloak/exceptions/group
{"chat": "-100123"} 
{"chat": "@groupname"}
```

Группа: live обязателен, в исключения добавляются участники (как в боте). В ответе поле `added`.

### `POST /v1/accounts/{id}/cloak/apply-all`

Скопировать enabled+текст **этого** аккаунта на все **запущенные** аккаунты. Как кнопка «применить ко всем».

---

## 11. Автоответ

Независимо от клоакинга. Если клоакинг включён, автоответ в рантайме не срабатывает (как в боте).

```http
GET /v1/accounts/{id}/autoreply
PUT /v1/accounts/{id}/autoreply
{"enabled": true, "text": "Привет, это автоответ"}

POST /v1/accounts/{id}/autoreply/exceptions
{"user_id": 111}

DELETE /v1/accounts/{id}/autoreply/exceptions/111
```

---

## 12. Глобальные настройки (на весь процесс)

Не привязаны к `account_id`. Меню «⚙️ Общие».

### `GET /v1/global`

```json
{
  "selected_account": "a1b2c3d4e5f6",
  "tag_enabled": false,
  "tag_text": "",
  "spam_disabled": false,
  "proxy_mode": "auto",
  "accounts": [ ]
}
```

### `PUT /v1/global/tag`

Текст дописывается в **конец каждого исходящего поста** (`\\n{тег}`), если `tag_enabled`.

```json
{ "enabled": true, "text": "#tag" }
```

Если передан непустой `text` без `enabled` — тег включается сам.

### `PUT /v1/global/spam`

```json
{ "disabled": true }
```

`disabled: true` — **глушит старт рассылки у всех** и сразу вызывает stop-all (как тумблер в боте). Снять: `"disabled": false`, затем снова `spam/start` по аккаунтам.

---

## 13. Прокси

Меню «🌐 Прокси». Пул общий. Пароли в API не отдаются (`masked`).

```http
GET  /v1/proxies
PUT  /v1/proxies/mode          {"mode": "auto"}   # auto | on | off
PUT  /v1/proxies               {"text": "host:port:user:pass\n..."}  # полная замена пула
POST /v1/proxies/check         # TCP-проверка (нужен живой процесс бота)
POST /v1/proxies/purge-bad
DELETE /v1/proxies             # очистить пул
```

`PUT /v1/proxies` и `POST /v1/proxies/check` до старта бота (пул ещё `None`) → `409`.

Формат строки: `host:port:user:password` (как в боте).

---

## 14. Служебное

| Метод | Путь | Ключ |
|---|---|---|
| GET | `/health` | нет |
| GET | `/v1/ping` | да |
| GET | `/docs` | нет (Swagger UI) |
| GET | `/redoc` | нет |
| GET | `/openapi.json` | нет |

Для агента достаточно скормить `/openapi.json` + ключ.

---

## 15. Соответствие кнопкам бота

| Бот | API |
|---|---|
| 👤 Аккаунты → ➕ | `POST /v1/accounts` |
| выбрать аккаунт | `POST …/select` (только UI мастера) |
| 🟢 Старт / 🔴 Стоп | `POST …/spam/start` · `…/stop` |
| 📮 Пост | `PUT …/post` |
| 💬 чат → вкл/выкл | `PATCH …/chats/{id}` `active` |
| 💬 свой текст | `PATCH` `text` |
| 💬 свой интервал | `PATCH` `interval` |
| 💬 стоп в чате | `PATCH` `spam_enabled: false` |
| 💬 покинуть | `POST …/chats/{id}/leave` |
| ⏱ интервалы | `PUT …/intervals/*` |
| 👁 упоминания | `PUT …/mentions` + `mention` у чата |
| 🛡 клоакинг | `PUT …/cloak` |
| 📨 автоответ | `PUT …/autoreply` |
| ⚙️ глобальный стоп | `PUT /v1/global/spam` |
| 🌐 прокси | `/v1/proxies` |

Не вынесено в API (осталось только в Telegram):

- захват **шаблона пересылки** (нужно переслать сообщение боту);
- сохранение **premium emoji / message entities** (API пишет голый текст);
- ручная загрузка `.session` диалогом FSM — заменена multipart/json.

---

## 16. Пример для другого бота / агента

```python
import httpx

BASE = "http://127.0.0.1:8787"
H = {"X-API-Key": "YOUR_KEY"}

with httpx.Client(timeout=120.0) as s:
    r = s.post(
        f"{BASE}/v1/accounts",
        headers=H,
        data={"bot_token": TOKEN, "label": "auto"},
        files={"session": ("a.session", open("a.session", "rb"))},
    )
    r.raise_for_status()
    aid = r.json()["account_id"]

    s.put(f"{BASE}/v1/accounts/{aid}/post", headers=H, json={
        "mode": "post", "text": "Оффер",
    }).raise_for_status()

    s.put(f"{BASE}/v1/accounts/{aid}/intervals/between", headers=H, json={
        "min_sec": 40, "max_sec": 90,
    }).raise_for_status()

    s.put(f"{BASE}/v1/accounts/{aid}/mentions", headers=H, json={"enabled": True})
    s.put(f"{BASE}/v1/accounts/{aid}/cloak", headers=H, json={
        "enabled": True, "text": "Клоак-ответ",
    })

    chats = s.get(f"{BASE}/v1/accounts/{aid}/chats", headers=H).json()["chats"]
    for ch in chats:
        s.patch(
            f"{BASE}/v1/accounts/{aid}/chats/{ch['chat_id']}",
            headers=H,
            json={"active": True},
        )

    s.post(f"{BASE}/v1/accounts/{aid}/spam/start", headers=H).raise_for_status()
```

Таймаут на `POST /v1/accounts` ставь **не меньше 90 с** — как в боте, бут сессии + проверка token могут занять минуту.

---

## 17. Поведение, которое легко сломать агентом

1. **`spam/start` заново включает `spam_enabled` у всех чатов.** Чтобы чат молчал постоянно — `active: false`, не `spam_enabled`.
2. **`mode: "global"` в PATCH стирает свой текст/фото/ссылку чата.** Сначала выставь mode, отдельным запросом — текст; или наоборот только `text` (он сам ставит mode=post).
3. **Ссылка `t.me` без live** → 409. Либо сначала `…/start`, либо передай `link_chat_id` + `link_msg_id`.
4. **Глобальный `spam_disabled`** блокирует start на всех аккаунтах, пока не снимешь `PUT /v1/global/spam {"disabled": false}`.
5. Слушай только `127.0.0.1`. Ключ = полный контроль над рассылкой и сессиями.
