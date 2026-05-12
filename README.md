# Vzaimno Backend

FastAPI backend + отдельный admin service, подготовленные к деплою на один DigitalOcean Droplet (Ubuntu 24.04, 2 vCPU / 4 GB RAM) без лишней инфраструктуры.

## Что в проекте

- `app/` — API для клиентов (iOS/Android)
- `services/admin_panel/` — админ-панель
- `alembic/` — миграции (единственный источник изменений схемы в проде)
- `scripts/entrypoint.sh` — pre-start (`wait-for-db` + optional migrations)
- `scripts/run_migrations.sh` — one-shot миграции для production compose flow
- `scripts/db_backup.sh`, `scripts/db_restore.sh` — dump/restore PostgreSQL
- `scripts/uploads_backup.sh`, `scripts/uploads_restore.sh` — backup/restore uploads
- `docker-compose.dev.yml` — локальная разработка
- `docker-compose.prod.yml` — прод на одном сервере
- `deploy/nginx/vzaimno.conf.example` — reverse proxy (2 домена + SSL)

## 0) SSH ключ для DigitalOcean (сделай перед созданием Droplet)

На твоем Mac:

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/do_vzaimno -C "do-vzaimno"
```

Публичный ключ для DigitalOcean:

```bash
cat ~/.ssh/do_vzaimno.pub
```

Добавь вывод команды в DigitalOcean: `Create -> Droplets -> Authentication -> SSH keys`.

Подключение к серверу после создания Droplet:

```bash
ssh -i ~/.ssh/do_vzaimno root@<DROPLET_IP>
```

## 1) Что нужно в DigitalOcean до деплоя

1. Создать Droplet: Ubuntu 24.04 LTS, Basic, 2 vCPU / 4 GB RAM.
2. Добавить SSH key (шаг выше).
3. Подключить домены:
   - `vzaimno.net` -> landing/backend
   - `api.vzaimno.net` -> backend API
   - `admin.vzaimno.net` -> admin
4. В DNS сделать `A` записи на IP Droplet.
5. Оплатить Droplet (иначе сервер не поднимется).

## 2) Первый деплой на сервер

### 2.1 Установить Docker + Compose plugin

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 2.2 Забрать код и заполнить прод-env

```bash
git clone <YOUR_REPO_URL> /opt/vzaimno_backend
cd /opt/vzaimno_backend
cp .env.production.example .env.production
```

Сгенерируй секреты:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Заполни обязательно в `.env.production`:

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `JWT_SECRET`
- `ADMIN_JWT_SECRET`
- `ADMIN_SESSION_SECRET`
- `IP_HASH_KEY`
- `PII_ENCRYPTION_KEY`
- `PHONE_HASH_KEY`
- `REDIS_URL`
- `TRUSTED_HOSTS` (`vzaimno.net,api.vzaimno.net,admin.vzaimno.net`)

### 2.3 Поднять стек

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d postgres redis uploads-init
docker compose -f docker-compose.prod.yml --env-file .env.production build backend admin
docker compose -f docker-compose.prod.yml --env-file .env.production --profile ops run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env.production up -d backend admin
```

Почему так:

- `backend` больше не запускает Alembic автоматически в production.
- миграции идут отдельным one-shot job `migrate`;
- внутри migration script есть Postgres advisory lock, чтобы не словить гонку при параллельном старте.

## 3) Проверка после деплоя

```bash
# backend readiness
curl -fsS http://127.0.0.1:8000/readyz

# admin readiness
curl -fsS http://127.0.0.1:8001/readyz

# logs
docker compose -f docker-compose.prod.yml --env-file .env.production logs -f backend admin
```

Если readiness `503`, сначала проверить `postgres`/`redis`:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

## 4) Nginx (vzaimno.net + API + admin + SSL)

Скопируй шаблон:

```bash
cp deploy/nginx/vzaimno.conf.example /etc/nginx/sites-available/vzaimno.conf
ln -s /etc/nginx/sites-available/vzaimno.conf /etc/nginx/sites-enabled/vzaimno.conf
nginx -t
systemctl reload nginx
```

Сертификаты (Let's Encrypt) выпусти для `vzaimno.net`, `api.vzaimno.net`, `admin.vzaimno.net`. Готовый чеклист для текущего Droplet и Cloudflare лежит в `deploy/digitalocean-vzaimno-net.md`.

## 5) Обновление без потери данных

```bash
cd /opt/vzaimno_backend
git pull
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/db_backup.sh
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/uploads_backup.sh /app/uploads
docker compose -f docker-compose.prod.yml --env-file .env.production build backend admin
docker compose -f docker-compose.prod.yml --env-file .env.production --profile ops run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --no-deps backend admin
```

Для одного droplet с обычным Docker Compose это безопаснее, чем `RUN_MIGRATIONS=1` на каждом старте backend. Полноценного rolling deployment Compose не даёт, но такой порядок минимизирует downtime и убирает риск одновременных миграций несколькими инстансами.

## 6) Бэкапы и восстановление

### Backup

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/db_backup.sh
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/uploads_backup.sh /app/uploads
```

### Restore

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/db_restore.sh /app/backups/db/<file>.dump
```

Важно:

- `db_restore.sh` теперь по умолчанию отказывается лить dump прямо в live production DB без `CONFIRM_PRODUCTION_DB_RESTORE=RESTORE_LIVE_DATABASE`;
- для restore drill лучше использовать отдельную временную БД через `TARGET_DATABASE_URL`;
- `uploads` бэкапятся отдельно tar-архивом и восстанавливаются либо в пустую директорию, либо только после явного подтверждения.

### Restore drill

1. Создать временную БД, например `vzaimno_restore_drill`.
2. Собрать `TARGET_DATABASE_URL` на эту БД с теми же credentials, но другим именем базы.
3. Восстановить dump в temp DB:

```bash
cd /opt/vzaimno_backend
TARGET_DATABASE_URL=postgresql://<user>:<password>@postgres:5432/vzaimno_restore_drill \
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend \
  ./scripts/db_restore.sh /app/backups/db/<file>.dump
```

4. Развернуть uploads в отдельную временную папку:

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend \
  ./scripts/uploads_restore.sh /app/backups/uploads/<file>.tar.gz /tmp/uploads_restore_drill
```

5. Проверить:
   - число таблиц/строк в temp DB;
   - наличие последних uploaded файлов;
   - что приложение может читать критичные данные (`users`, `announcements`, media metadata).

6. Зафиксировать дату drill и путь к последнему валидному backup.

## 7) Базовый rollback

1. Остановить сервисы:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production stop backend admin
```

2. Вернуть предыдущий commit:

```bash
git checkout <PREVIOUS_COMMIT>
```

3. Поднять снова:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build backend admin
```

4. Если нужно, восстановить БД из backup.

## 8) Как удалять сервер и не платить

Если не пользуешься сервером:

1. Сделай backup БД + uploads.
2. Удали Droplet в DigitalOcean.
3. Чтобы запустить снова — создай новый Droplet, восстанови backups, снова `docker compose up`.

Удаление Droplet останавливает списания за compute. Отдельно проверь, не остались ли платные ресурсы (Volumes, Floating IP, snapshots, managed DB).

## 9) Локальная разработка

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d --build
```

- Backend: `http://localhost:8000`
- Admin: `http://localhost:8001/admin/users`

## Security/Deployment notes

- `ensure_all_tables()` больше не делает runtime-schema bootstrap по умолчанию.
- Схема в проде меняется только через Alembic (`alembic upgrade head`).
- Для production compose миграции запускаются отдельным `migrate` job, а не из `backend`.
- `admin /uploads` не публичен, доступ только для авторизованного staff.
- Добавлены `healthz` и `readyz` для backend/admin.
- Добавлены trusted hosts, proxy headers и CORS по env.
- Добавлен rate limiting для `/auth/login` и `/auth/register`.

## 10) Auth, PII и uploads: что проверять при сбоях

### Пользовательская авторизация

- `/auth/register` и `/auth/login` теперь возвращают `access_token` и `refresh_token`.
- `/auth/refresh` принимает refresh token, проверяет его hash в `user_sessions`, ротирует refresh token и выдаёт новый access token.
- `/auth/logout` отзывает refresh token; `/auth/sessions/revoke` отзывает конкретную session id; `/auth/sessions/revoke-all` отзывает все активные user sessions.
- Login audit пишется в `login_attempts`; после `LOGIN_LOCK_THRESHOLD` неверных попыток аккаунт блокируется на `LOGIN_LOCK_DURATION_MINUTES`.
- Для несуществующего email выполняется dummy bcrypt-check, чтобы снизить риск timing enumeration.
- Password reset хранит только `token_hash` в `password_reset_tokens`; confirm сбрасывает пароль и отзывает старые сессии. В dev можно временно включить выдачу reset token в ответе через `ALLOW_DEV_PASSWORD_RESET_TOKEN=1`.

Если логин внезапно возвращает `423`, проверь `users.locked_until`, `failed_login_attempts` и последние строки `login_attempts`. Если refresh не работает, проверь, что `user_sessions.revoked_at IS NULL`, `expires_at > now()` и клиент использует самый свежий refresh token после ротации.

### DB pool и транзакции

- `app/db.py` использует `psycopg_pool`, а старые helpers `fetch_one`, `fetch_all`, `execute` продолжают работать.
- Для критичных цепочек используется `transaction()`: внутри неё все helpers идут через одну connection и общий commit/rollback.
- Размер пула задаётся `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_MAX_IDLE_SECONDS`.

Если API начинает отвечать медленно под нагрузкой, проверь метрики pool wait/in-use и увеличивай `DB_POOL_MAX_SIZE` только вместе с лимитом `max_connections` Postgres.

### PII

- `users.phone_enc` читается через `pgp_sym_decrypt(..., PII_ENCRYPTION_KEY)`.
- Поиск/сравнение телефона должно идти через `phone_hash`, который считается HMAC-SHA256 с `PHONE_HASH_KEY`.
- В production обязательны `PII_ENCRYPTION_KEY`, `PHONE_HASH_KEY` и `IP_HASH_KEY`.

Если телефоны отображаются пустыми, сначала проверь наличие `PII_ENCRYPTION_KEY` в окружении сервиса и то, что миграция `0004_users_phone_encrypted` уже применена.

### Uploads

- App-level лимит размера задаётся `UPLOAD_MAX_FILE_SIZE_BYTES`, количество файлов в одном запросе — `UPLOAD_MAX_FILE_COUNT`.
- Разрешены только JPEG, PNG и WebP; тип проверяется по magic bytes, расширению, заявленному MIME и через Pillow.
- `UPLOAD_MAX_IMAGE_PIXELS` ограничивает decompression bombs.
- Сохранение идёт через `app/storage.py`; local storage дополнительно блокирует path traversal.

Если клиент получает `413`, файл слишком большой или картинка превышает pixel guard. Если `400`, проверь фактический формат файла и расширение, а не только `Content-Type`.

### Admin CSRF

- Staff POST-формы под `/admin` требуют hidden `csrf_token`, который хранится в cookie-session.
- JSON API под `/admin/api` не блокируется form-CSRF middleware и должен защищаться bearer/session auth.

Если staff POST возвращает `403 CSRF token missing or invalid`, открой страницу формы заново, чтобы получить свежую session cookie и hidden token.

### Nginx

- Пример `deploy/nginx/vzaimno.conf.example` теперь включает HSTS, CSP/frame headers, COOP/CORP, timeouts, gzip, ACME path, `X-Forwarded-*` headers и отдельные `limit_req`/`limit_conn` зоны для auth, admin, uploads и reports.
- `api` домен теперь явно не обслуживает `/admin/*`, а `admin` домен не проксирует произвольный `/` на backend/admin без явного маршрута.
- После правки production config всегда проверяй синтаксис: `sudo nginx -t`, затем reload.

## 11) Production checklists

### Deploy checklist

- `.env.production` создан из `.env.production.example`, но реальные секреты не лежат в git.
- `TRUSTED_HOSTS` содержит оба production-домена.
- `DATABASE_URL` указывает на `postgres` внутри compose-сети, не на localhost хоста.
- `JWT_SECRET`, `ADMIN_JWT_SECRET`, `ADMIN_SESSION_SECRET`, `IP_HASH_KEY`, `PII_ENCRYPTION_KEY`, `PHONE_HASH_KEY` заполнены сильными случайными значениями.
- `docker compose -f docker-compose.prod.yml --env-file .env.production config` проходит без ошибок.
- `docker compose -f docker-compose.prod.yml --env-file .env.production --profile ops run --rm migrate` отрабатывает отдельно до рестарта app services.
- `curl -fsS http://127.0.0.1:8000/readyz` и `curl -fsS http://127.0.0.1:8001/readyz` возвращают ready.
- `sudo nginx -t` проходит, после reload оба домена открываются по HTTPS.
- Есть свежий DB backup и свежий uploads backup перед каждым обновлением.

### Backup/restore checklist

- Хранить минимум один свежий `*.dump` и один свежий `uploads_*.tar.gz` вне сервера.
- Для каждого backup сохранять checksum (`*.sha256`).
- Не тестировать restore сразу в live DB; сначала temp DB / temp directory.
- Проверять, что temp restore содержит актуальные таблицы, media и admin-доступ.
- Проводить restore drill регулярно и отмечать дату последней успешной проверки.

### Env checklist

Обязательные для production:

- `ENV`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `REDIS_URL`
- `JWT_SECRET`
- `ADMIN_JWT_SECRET`
- `ADMIN_SESSION_SECRET`
- `TRUSTED_HOSTS`
- `IP_HASH_KEY`
- `PII_ENCRYPTION_KEY`
- `PHONE_HASH_KEY`

Обычно нужны в production:

- `ENABLE_PROXY_HEADERS`
- `FORWARDED_ALLOW_IPS`
- `CORS_ALLOWED_ORIGINS`
- `ADMIN_CORS_ALLOWED_ORIGINS`
- `UPLOADS_DIR`
- `STORAGE_BACKEND`
- `UPLOAD_MAX_FILE_SIZE_BYTES`
- `UPLOAD_MAX_FILE_COUNT`
- `UPLOAD_MAX_IMAGE_PIXELS`
- `JWT_ALG`
- `ADMIN_JWT_ALG`
- `JWT_EXPIRE_MINUTES`
- `ADMIN_JWT_EXPIRE_MINUTES`
- `ADMIN_SESSION_COOKIE_NAME`
- `ADMIN_SESSION_COOKIE_SECURE`
- `ADMIN_SESSION_COOKIE_SAMESITE`
- `ADMIN_SESSION_MAX_AGE_SECONDS`
- `DB_CONNECT_TIMEOUT_SECONDS`
- `DB_POOL_MIN_SIZE`
- `DB_POOL_MAX_SIZE`
- `DB_POOL_TIMEOUT_SECONDS`
- `DB_POOL_MAX_IDLE_SECONDS`

Опционально, если фича включена:

- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`
- `S3_REGION`
- `S3_BUCKET`
- `S3_PRESIGNED_EXPIRES_SECONDS`
- `S3_PRESIGNED_EXPIRES_SECONDS_MAX`
- `YANDEX_ROUTING_API_KEY`
- `NOMINATIM_URL`
- `GEOCODER_USER_AGENT`
- `GEOCODE_ON_CREATE`
- `GEOCODE_ON_CREATE_TIMEOUT_SECONDS`
- `ROUTE_EXTERNAL_GEOCODE_ENABLED`
- `ROUTE_DEFAULT_TRAVEL_MODE`
- `ROUTE_GEOCODE_TIMEOUT_SECONDS`
- `ROUTE_TASKS_LIMIT`
- `ROUTE_TASK_RADIUS_METERS`
- `DISPUTE_GROQ_API_KEY`
- `DISPUTE_GROQ_BASE_URL`
- `DISPUTE_GROQ_MODEL`
- `DISPUTE_GROQ_PROMPT`
- `DISPUTE_GROQ_TIMEOUT_S`
- `DISPUTE_GROQ_RETRIES`
- `OLLAMA_ENABLED`
- `OLLAMA_URL`
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT`
- `OLLAMA_TIMEOUT_S`
- `OLLAMA_RETRIES`
- `NSFW_MODEL_ID`
- `NSFW_DEVICE`
- `NSFW_CACHE_DIR`
- `MODEL_CACHE_DIR`
- `NSFW_REVIEW`
- `NSFW_HARD_BLOCK`
- `OTEL_EXPORTER_OTLP_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TIMEOUT_S`
- `OTEL_SERVICE_NAME`
- `APP_GIT_SHA`
- `INSTANCE_ID`
- `ADMIN_BASE_URL`
- `ADMIN_TITLE`
- `ADMIN_BOOTSTRAP_LOGIN`
- `ADMIN_BOOTSTRAP_PASSWORD`
- `ADMIN_BOOTSTRAP_DISPLAY_NAME`
- `LOGIN_LOCK_THRESHOLD`
- `LOGIN_LOCK_DURATION_MINUTES`
- `REFRESH_EXPIRE_DAYS`
- `USER_REFRESH_EXPIRE_DAYS`
- `ADMIN_REFRESH_EXPIRE_DAYS`
- `PASSWORD_RESET_EXPIRE_MINUTES`
