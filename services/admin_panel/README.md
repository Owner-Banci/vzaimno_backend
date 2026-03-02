# Admin Panel Service

Отдельный FastAPI-сервис для модерации объявлений, очереди апелляций, support inbox, reports, audit log и user restrictions.

## Что использует

- общую `DATABASE_URL`
- общие `JWT_SECRET` / `JWT_ALG`
- sqladmin auth backend через `/admin/login`

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

- `GET /admin/api/support/threads`
- `GET /admin/api/support/threads/{thread_id}/messages`
- `POST /admin/api/support/threads/{thread_id}/messages`
- `POST /admin/api/announcements/{ann_id}/decision`
- `POST /admin/api/reports/{report_id}/resolve`
- `POST /admin/api/restrictions`
- `POST /admin/api/restrictions/{restriction_id}/revoke`
- `POST /admin/api/users/{user_id}/role`
