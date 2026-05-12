# DigitalOcean deploy for vzaimno.net

This checklist is for the current Droplet:

- Public IPv4: `134.122.77.212`
- Public IPv6: `2a03:b0c0:3:f0:0:2:638a:c000`
- Root domain/site: `vzaimno.net`
- API domain: `api.vzaimno.net`
- Admin domain: `admin.vzaimno.net`

## 1. Cloudflare DNS panel

Open Cloudflare -> `vzaimno.net` -> DNS -> Records.

Replace the old `146.190.241.3` records and add the missing apex record:

| Type | Name | Content | Proxy | TTL |
| --- | --- | --- | --- | --- |
| A | `@` | `134.122.77.212` | DNS only while issuing certs | Auto |
| A | `api` | `134.122.77.212` | DNS only while issuing certs | Auto |
| A | `admin` | `134.122.77.212` | DNS only while issuing certs | Auto |

Optional IPv6 records:

| Type | Name | Content | Proxy | TTL |
| --- | --- | --- | --- | --- |
| AAAA | `@` | `2a03:b0c0:3:f0:0:2:638a:c000` | DNS only while issuing certs | Auto |
| AAAA | `api` | `2a03:b0c0:3:f0:0:2:638a:c000` | DNS only while issuing certs | Auto |
| AAAA | `admin` | `2a03:b0c0:3:f0:0:2:638a:c000` | DNS only while issuing certs | Auto |

After Let's Encrypt certificates are issued and nginx reloads successfully, you can switch Cloudflare proxy to proxied/orange cloud if desired.

## 2. DigitalOcean Web Console: enable SSH key

The Droplet currently rejects the local `do_vzaimno` key. In DigitalOcean -> Droplet -> Web Console, log in as `root` and run:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICHbf5PzCFOfvs0fC0hqEhyEkHmfRuNtzX9XHAEhp6pY do-vzaimno' >> ~/.ssh/authorized_keys
sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

After this, SSH from the Mac should work:

```bash
ssh -i ~/.ssh/do_vzaimno root@134.122.77.212
```

## 3. Server bootstrap commands

Run these on the Droplet as `root`.

```bash
set -euo pipefail

apt-get update
apt-get install -y ca-certificates curl gnupg git nginx certbot python3-certbot-nginx

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

git clone https://github.com/Owner-Banci/vzaimno_backend.git /opt/vzaimno_backend
cd /opt/vzaimno_backend
cp .env.production.example .env.production
```

Edit `/opt/vzaimno_backend/.env.production` and set real secrets. Minimum required values:

```env
POSTGRES_PASSWORD=<strong-db-password>
DATABASE_URL=postgresql://vzaimno:<strong-db-password>@postgres:5432/vzaimno
JWT_SECRET=<random-secret>
ADMIN_JWT_SECRET=<random-secret>
ADMIN_SESSION_SECRET=<random-secret>
IP_HASH_KEY=<random-secret>
PII_ENCRYPTION_KEY=<random-secret>
PHONE_HASH_KEY=<random-secret>
REDIS_URL=redis://redis:6379/0
TRUSTED_HOSTS=vzaimno.net,api.vzaimno.net,admin.vzaimno.net
CORS_ALLOWED_ORIGINS=https://vzaimno.net,https://api.vzaimno.net
ADMIN_CORS_ALLOWED_ORIGINS=https://admin.vzaimno.net
GEOCODER_USER_AGENT=vzaimno-backend/1.0 (contact: admin@vzaimno.net)
```

Generate secrets with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 4. Start app stack

```bash
cd /opt/vzaimno_backend
docker compose -f docker-compose.prod.yml --env-file .env.production up -d postgres redis uploads-init
docker compose -f docker-compose.prod.yml --env-file .env.production build backend admin
docker compose -f docker-compose.prod.yml --env-file .env.production --profile ops run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env.production up -d backend admin
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Check local readiness:

```bash
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8001/readyz
```

## 5. Nginx and HTTPS

```bash
cd /opt/vzaimno_backend
cp deploy/nginx/vzaimno.conf.example /etc/nginx/sites-available/vzaimno.conf
ln -sf /etc/nginx/sites-available/vzaimno.conf /etc/nginx/sites-enabled/vzaimno.conf
rm -f /etc/nginx/sites-enabled/default

mkdir -p /var/www/certbot
certbot certonly --webroot -w /var/www/certbot \
  -d vzaimno.net \
  -d api.vzaimno.net \
  -d admin.vzaimno.net \
  --email admin@vzaimno.net \
  --agree-tos \
  --no-eff-email

nginx -t
systemctl reload nginx
```

Public checks:

```bash
curl -fsS https://vzaimno.net/
curl -fsS https://api.vzaimno.net/readyz
curl -fsS https://admin.vzaimno.net/readyz
```

