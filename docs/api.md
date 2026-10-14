# Marketing HTTP API

Крутится **в том же процессе**, что и бот (`main.py`). По умолчанию: `http://127.0.0.1:8791`
(порт **8790** на сервере занят outreachchats).

Публично (когда DNS готов): `https://marketingapi.arix.vu` → nginx → `127.0.0.1:8791`.

Интерактивная схема: `/docs` · OpenAPI: `/openapi.json`.

## Авторизация

Все пути `/v1/*` требуют ключ. `/health` — без ключа.

| Заголовок | Пример |
|---|---|
| `X-API-Key` | `X-API-Key: <ключ>` |
| `Authorization` | `Authorization: Bearer <ключ>` |

Ключ: `API_KEY` в `.env` или файл `data/api_key.txt` (создаётся сам при первом старте).

## Нужен ли домен?

**Нет**, если вызываете с той же машины или по IP:порту (`http://77.x.x.x:8790` за файрволом).

**Да**, если нужен доступ снаружи по HTTPS/красивому имени — тогда nginx/caddy + домен + TLS. Не открывайте `0.0.0.0` без ключа и файрвола.

## Эндпоинты (все под `/v1`, кроме `/health`)

| Метод | Путь | Что отдаёт |
|---|---|---|
| GET | `/health` | жив ли процесс |
| GET | `/v1/accounts` | каталог аккаунтов (+ premium_until) |
| GET | `/v1/accounts/{id}` | один аккаунт |
| GET | `/v1/chats` | чаты |
| GET | `/v1/chats/{pk}` | один чат |
| GET | `/v1/schedule` | все минутные слоты |
| GET | `/v1/schedule/{chat_pk}` | слоты чата |
| GET | `/v1/settings/sender` | интервалы / клоакинг |
| GET | `/v1/reports/dashboard?day=` | отчётность + html |
| GET | `/v1/reports/facts?day=` | факты |
| GET | `/v1/reports/density?day=` | частота по чатам |
| GET | `/v1/reports/accounts?day=&page=` | по аккаунтам |
| GET | `/v1/reports/leads?day=` | люди в ЛС |
| GET | `/v1/reports/coverage` | покрытие / дефицит |
| GET | `/v1/reports/advisor` | советы |
| GET | `/v1/reports/marketer?act=false` | цикл оператора + brief |
| GET | `/v1/reports/redesign` | для переоформления |
| GET | `/v1/reports/export` | полный export-словарь |
| GET | `/v1/incidents` | проблемы |
| GET | `/v1/jobs` | журнал задач |
| GET | `/v1/restrictions` | муты/баны/события |
| GET | `/v1/facts/window?minutes=60` | сетка фактов |
| GET | `/v1/facts/day?days_ago=0` | сутки |
| GET | `/v1/send-events?since=` | сырые отправки |
| GET | `/v1/chat-facts` | пары аккаунт×чат |
| GET | `/v1/dm/people` | люди в ЛС |
| GET | `/v1/premium` | статус Premium / до когда / warn |
| GET | `/v1/setup-states` | статусы настройки |

Пример:

```bash
curl -H "X-API-Key: $(cat data/api_key.txt)" http://127.0.0.1:8791/v1/reports/dashboard
```
