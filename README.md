# Vzaimno Backend

FastAPI backend + отдельный admin service, подготовленные к деплою на один DigitalOcean Droplet (Ubuntu 24.04, 2 vCPU / 4 GB RAM) без лишней инфраструктуры.

## Что в проекте

- `app/` — API для клиентов (iOS/Android)
- `services/admin_panel/` — админ-панель
- `alembic/` — миграции (единственный источник изменений схемы в проде)
- `scripts/entrypoint.sh` — pre-start (`wait-for-db` + optional migrations)
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
3. Купить/подключить домены (минимум 2):
   - `api.your-domain.com` -> backend
   - `admin.your-domain.com` -> admin
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
- `TRUSTED_HOSTS` (оба домена)

### 2.3 Поднять стек

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

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

## 4) Nginx (2 адреса + SSL)

Скопируй шаблон:

```bash
cp deploy/nginx/vzaimno.conf.example /etc/nginx/sites-available/vzaimno.conf
ln -s /etc/nginx/sites-available/vzaimno.conf /etc/nginx/sites-enabled/vzaimno.conf
nginx -t
systemctl reload nginx
```

Сертификаты (Let's Encrypt) выпусти для обоих доменов и подставь пути в конфиг.

## 5) Обновление без потери данных

```bash
cd /opt/vzaimno_backend
git pull
docker compose -f docker-compose.prod.yml --env-file .env.production build backend admin
docker compose -f docker-compose.prod.yml --env-file .env.production up -d backend admin
```

Миграции запускаются pre-start у `backend` (`RUN_MIGRATIONS=1`).

## 6) Бэкапы и восстановление

### Backup

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/db_backup.sh
```

### Restore

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend ./scripts/db_restore.sh /app/backups/<file>.dump
```

Важно: volume `postgres_data` и `uploads_data` должны бэкапиться отдельно (snapshot/rsync/tar).

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
<<<<<<< HEAD
cd ../pg-docker

docker compose up -d
# порт 127.0.0.1:5433 проброшен на host, извне LAN недоступен
=======
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d --build
>>>>>>> 947dee1 (Исправил ошибки из-за которых падал сервер)
```

- Backend: `http://localhost:8000`
- Admin: `http://localhost:8001/admin/users`

## Security/Deployment notes

- `ensure_all_tables()` больше не делает runtime-schema bootstrap по умолчанию.
- Схема в проде меняется только через Alembic (`alembic upgrade head`).
- `admin /uploads` не публичен, доступ только для авторизованного staff.
- Добавлены `healthz` и `readyz` для backend/admin.
- Добавлены trusted hosts, proxy headers и CORS по env.
- Добавлен rate limiting для `/auth/login` и `/auth/register`.
