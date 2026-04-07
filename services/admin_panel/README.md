# Admin Panel Service

Отдельный FastAPI-сервис для модерации объявлений, очереди апелляций, support inbox, reports, audit log и user restrictions.

## Что использует

- общую `DATABASE_URL`
- отдельные admin JWT claims через `ADMIN_JWT_SECRET` / `ADMIN_JWT_ALG` и `principal_type=admin`
- sqladmin auth backend через `/admin/login`
- отдельные `admin_accounts` и `admin_sessions`, а не `users.role = admin`

## Установка

```bash
pip install -r services/admin_panel/requirements.txt
```

## Запуск

```bash
uvicorn services.admin_panel.app.main:app --host 0.0.0.0 --port 8001 --reload
```

## Основные разделы

- `/admin/users/`
- `/admin/announcements/`
- `/admin/reports/`
- `/admin/support/`
- `/admin/restrictions/`
- `/admin/audit/`

## JSON API

- `POST /admin/api/auth/login`
- `POST /admin/api/auth/logout`
- `GET /admin/api/auth/me`
- `GET /admin/api/support/threads`
- `GET /admin/api/support/threads/{thread_id}/messages`
- `POST /admin/api/support/threads/{thread_id}/messages`
- `POST /admin/api/support/threads/{thread_id}/assign`
- `GET /admin/api/users/{user_id}/admin-access`
- `POST /admin/api/users/{user_id}/admin-access`
- `POST /admin/api/admin-accounts/{admin_account_id}/disable`
- `POST /admin/api/admin-accounts/{admin_account_id}/enable`
- `POST /admin/api/admin-accounts/{admin_account_id}/reset-credentials`
- `POST /admin/api/announcements/{ann_id}/decision`
- `POST /admin/api/reports/{report_id}/resolve`
- `POST /admin/api/restrictions`
- `POST /admin/api/restrictions/{restriction_id}/revoke`
