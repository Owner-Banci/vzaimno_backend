# Vzaimno Backend

FastAPI-сервис для размещения объявлений и их выполнения ("взаимные задачи"). Стадия: MVP. Клиенты: iOS (`DelegationApp`) и Android (`Vzaimno_UI`).

## Структура

```
app/                        # основной backend (FastAPI)
  main.py                   # все роуты, FastAPI app
  security.py               # JWT, bcrypt-sha256
  auth_context.py           # get_current_user dependency
  db.py                     # psycopg3 (один глобальный коннект — MVP)
  bootstrap.py              # DDL при старте (временная замена Alembic)
  chat.py                   # WebSocket-чаты (in-memory hub)
  moderation_image.py       # NSFW (timm)
  moderation_text.py        # Ollama LLM
  geocoding.py              # Nominatim OSM
  routes_module/            # построение маршрутов (Yandex API)
  audit.py                  # аудит-лог в БД
services/admin_panel/       # отдельное FastAPI + SQLAdmin приложение
uploads/                    # локальное хранилище медиа (dev)
tests/                      # pytest
```

Соседний репозиторий `pg-docker/` поднимает Postgres + PostGIS в контейнере.

## Как запустить (dev)

### 1. Поднять Postgres

```bash
cd ../pg-docker

docker compose up -d
# порт 127.0.0.1:5433 проброшен на host, извне LAN недоступен
```

Создать базу `vzaimno` (по умолчанию создаётся `appdb`):

```bash
docker exec -it pg_local psql -U app -d appdb -c "CREATE DATABASE vzaimno;"
```

### 2. Подготовить `.env` backend

```bash
cd ../vzaimno_backend
cp .env.example .env
```

Обязательно заполни:

- `DATABASE_URL` — тот же пароль, что в `pg-docker/.env`.
- `JWT_SECRET` — сгенерируй длинный случайный секрет:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
  **Backend откажется стартовать**, если `JWT_SECRET` пуст или совпадает с известными placeholder-значениями (`CHANGE_ME_SUPER_SECRET`, `DEV_JWT_SECRET_CHANGE_ME`, `CHANGE_ME`).
- `YANDEX_ROUTING_API_KEY` — получи новый на https://developer.tech.yandex.ru/services.

### 3. Поставить зависимости и запустить

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi 'uvicorn[standard]' 'psycopg[binary]' python-dotenv \
            'python-jose[cryptography]' bcrypt 'passlib[bcrypt]' \
            pydantic python-multipart itsdangerous anyio \
            timm torch pillow \
            sqlalchemy sqladmin jinja2

uvicorn app.main:app --reload --port 8000
```

Важно для realtime-чата: запускай именно `uvicorn` из `.venv` (после `source .venv/bin/activate`), иначе может не подхватиться websocket-транспорт и в логах появятся предупреждения:
`Unsupported upgrade request` / `No supported WebSocket library detected`.

Админ-панель (отдельный процесс):

```bash
cd services/admin_panel
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

## Что изменилось в безопасности (важное)

| Было | Стало |
|---|---|
| `if token == "DEV_TOKEN"` — вход в любом окружении | Принимается только при `ENV=dev` **И** `ALLOW_DEV_TOKEN=1`. По умолчанию bypass выключен. |
| `JWT_SECRET = os.getenv("JWT_SECRET", "DEV_JWT_SECRET_CHANGE_ME")` | `RuntimeError` при пустом/placeholder-значении — backend не стартует. |
| `app.mount("/uploads", StaticFiles)` — любой может скачать | `GET /uploads/{ann_id}/{filename}` требует JWT и проверяет: владелец/назначенный исполнитель/публичное объявление. Path traversal блокируется. |
| `.env` закоммичен | `.env` в `.gitignore`; есть `.env.example` как template. |
| Postgres на `0.0.0.0:5433` | Bound на `127.0.0.1:5433` (loopback-only), есть `networks:` и `healthcheck`. |

## Тесты

```bash
pytest tests/
```

## Что делать дальше (roadmap)

- [ ] Alembic вместо runtime-DDL в `bootstrap.py`.
- [ ] Refresh-токены (схема `user_sessions.refresh_token_hash` уже готова).
- [ ] `psycopg_pool.ConnectionPool` вместо глобального коннекта.
- [ ] Rate limiting (`slowapi`) на `/auth/login` и `/auth/register`.
- [ ] `CORSMiddleware` с явным allowlist.
- [ ] Dockerfile backend + объединённый compose-стек.
- [ ] Sentry + структурированные JSON-логи.
- [ ] S3-compatible object storage для `uploads/`.
