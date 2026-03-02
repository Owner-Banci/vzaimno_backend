from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, Request
from sqladmin import BaseView, expose
from starlette.responses import RedirectResponse

from . import crud
from .auth import require_admin_user, require_staff_user
from .db import SessionLocal

UI_LABELS = {
    "roles": {
        "user": "Пользователь",
        "support": "Поддержка",
        "moderator": "Модератор",
        "admin": "Администратор",
    },
    "announcement_statuses": {
        "pending_review": "На проверке",
        "needs_fix": "Нужно исправить",
        "rejected": "Отклонено",
        "active": "Активно",
        "archived": "В архиве",
        "draft": "Черновик",
    },
    "restriction_types": {
        "warning": "Предупреждение",
        "ban": "Блокировка",
        "shadowban": "Теневая блокировка",
    },
    "restriction_statuses": {
        "active": "Активно",
        "revoked": "Снято",
    },
    "report_statuses": {
        "open": "Открыта",
        "resolved": "Закрыта",
    },
    "report_resolutions": {
        "valid": "Обоснована",
        "invalid": "Не обоснована",
    },
    "target_types": {
        "announcement": "Объявление",
        "message": "Сообщение",
        "user": "Пользователь",
        "task": "Задание",
        "report": "Жалоба",
    },
    "sender_roles": {
        "user": "Пользователь",
        "support": "Поддержка",
        "moderator": "Модератор",
        "admin": "Администратор",
    },
    "actions": {
        "approve": "Одобрить",
        "needs_fix": "Отправить на доработку",
        "reject": "Отклонить",
        "archive": "Архивировать",
        "delete": "Удалить",
        "report_resolve": "Решение по жалобе",
        "restriction_set": "Назначено ограничение",
        "restriction_revoke": "Ограничение снято",
        "user_role_change": "Смена роли",
    },
}


def _format_dt(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return "—"
        try:
            normalized = raw.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    staff_user = require_staff_user(request)
    context = {
        "request": request,
        "staff_user": staff_user,
        "is_admin": staff_user.role == "admin",
        "admin_base_url": "/admin",
        "labels": UI_LABELS,
        "format_dt": _format_dt,
    }
    context.update(extra)
    return context


async def _render(view: BaseView, request: Request, template_name: str, **extra: Any):
    templates = view.templates
    return await templates.TemplateResponse(request, template_name, _base_context(request, **extra))


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _parse_optional_dt(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class UsersView(BaseView):
    name = "Пользователи"
    identity = "users"
    icon = "fa-solid fa-users"

    @expose("/users", methods=["GET"], identity="users")
    async def a_index(self, request: Request):
        search = (request.query_params.get("search") or "").strip()
        with SessionLocal() as session:
            users = crud.list_users(session, search or None)
        return await _render(self, request, "users.html", users=users, search=search)

    @expose("/users/{user_id}", methods=["GET"], identity="users-detail")
    async def user_detail(self, request: Request):
        user_id = request.path_params["user_id"]
        with SessionLocal() as session:
            try:
                user = crud.get_user_detail(session, user_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render(self, request, "user_detail.html", user=user)

    @expose("/users/{user_id}/role", methods=["POST"], identity="users-update-role")
    async def update_role(self, request: Request):
        actor = require_admin_user(request)
        user_id = request.path_params["user_id"]
        form = await request.form()
        role = str(form.get("role", "")).strip()
        with SessionLocal() as session:
            try:
                crud.update_user_role(session, user_id, role, actor.id, actor.role)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"/admin/users/{user_id}")


class AnnouncementsModerationView(BaseView):
    name = "Модерация"
    identity = "announcements"
    icon = "fa-solid fa-shield-halved"

    @expose("/announcements", methods=["GET"], identity="announcements")
    async def a_index(self, request: Request):
        status_filter = (request.query_params.get("status") or "").strip() or None
        appeals_only = (request.query_params.get("appeals") or "").strip() == "1"
        search = (request.query_params.get("search") or "").strip()
        with SessionLocal() as session:
            items = crud.list_moderation_announcements(
                session=session,
                status_filter=status_filter,
                appeals_only=appeals_only,
                search=search or None,
            )
        return await _render(
            self,
            request,
            "moderation_list.html",
            announcements=items,
            status_filter=status_filter or "",
            appeals_only=appeals_only,
            search=search,
        )

    @expose("/announcements/{ann_id}", methods=["GET"], identity="announcements-detail")
    async def announcement_detail(self, request: Request):
        ann_id = request.path_params["ann_id"]
        with SessionLocal() as session:
            try:
                announcement = crud.get_announcement_detail(session, ann_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render(self, request, "moderation_detail.html", announcement=announcement)

    @expose("/announcements/{ann_id}/decision", methods=["POST"], identity="announcements-decision")
    async def apply_decision(self, request: Request):
        actor = require_staff_user(request)
        ann_id = request.path_params["ann_id"]
        form = await request.form()
        decision = str(form.get("decision", "")).strip()
        message = str(form.get("message", "")).strip()
        reason_field = str(form.get("reason_field", "")).strip()
        reason_code = str(form.get("reason_code", "")).strip()
        reason_details = str(form.get("reason_details", "")).strip()
        can_appeal = str(form.get("can_appeal", "true")).lower() != "false"
        suggestions_raw = str(form.get("suggestions", "")).strip()
        reasons = []
        if reason_field and reason_code and reason_details:
            reasons.append(
                {
                    "field": reason_field,
                    "code": reason_code,
                    "details": reason_details,
                    "can_appeal": can_appeal,
                }
            )
        suggestions = [line.strip() for line in suggestions_raw.splitlines() if line.strip()]
        with SessionLocal() as session:
            try:
                crud.apply_announcement_decision(
                    session=session,
                    ann_id=ann_id,
                    moderator_id=actor.id,
                    decision=decision,
                    message=message,
                    reasons=reasons,
                    suggestions=suggestions,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"/admin/announcements/{ann_id}")


class ReportsView(BaseView):
    name = "Жалобы"
    identity = "reports"
    icon = "fa-solid fa-flag"

    @expose("/reports", methods=["GET"], identity="reports")
    async def a_index(self, request: Request):
        search = (request.query_params.get("search") or "").strip()
        status = (request.query_params.get("status") or "open").strip() or None
        with SessionLocal() as session:
            reports = crud.list_reports(session, search=search or None, status=status)
        return await _render(self, request, "reports_list.html", reports=reports, search=search, status=status or "")

    @expose("/reports/{report_id}", methods=["GET"], identity="reports-detail")
    async def report_detail(self, request: Request):
        report_id = request.path_params["report_id"]
        with SessionLocal() as session:
            try:
                report = crud.get_report_detail(session, report_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render(self, request, "report_detail.html", report=report)

    @expose("/reports/{report_id}/resolve", methods=["POST"], identity="reports-resolve")
    async def resolve(self, request: Request):
        actor = require_staff_user(request)
        report_id = request.path_params["report_id"]
        form = await request.form()
        resolution = str(form.get("resolution", "")).strip()
        moderator_comment = str(form.get("moderator_comment", "")).strip() or None
        with SessionLocal() as session:
            try:
                crud.resolve_report(session, report_id, actor.id, resolution, moderator_comment)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"/admin/reports/{report_id}")


class SupportThreadsView(BaseView):
    name = "Поддержка"
    identity = "support"
    icon = "fa-solid fa-headset"

    @expose("/support", methods=["GET"], identity="support")
    async def a_index(self, request: Request):
        search = (request.query_params.get("search") or "").strip()
        with SessionLocal() as session:
            threads = crud.list_support_threads(session, search or None)
        return await _render(self, request, "support_threads.html", threads=threads, search=search)

    @expose("/support/{thread_id}", methods=["GET"], identity="support-thread")
    async def thread_view(self, request: Request):
        thread_id = request.path_params["thread_id"]
        with SessionLocal() as session:
            try:
                thread = crud.get_support_thread(session, thread_id)
                messages = crud.get_support_messages(session, thread_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render(self, request, "support_thread.html", thread=thread, messages=messages)

    @expose("/support/{thread_id}/reply", methods=["POST"], identity="support-reply")
    async def reply(self, request: Request):
        actor = require_staff_user(request)
        thread_id = request.path_params["thread_id"]
        form = await request.form()
        text_value = str(form.get("text", "")).strip()
        with SessionLocal() as session:
            try:
                crud.post_support_message(session, thread_id, actor.id, text_value)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"/admin/support/{thread_id}")


class RestrictionsView(BaseView):
    name = "Ограничения"
    identity = "restrictions"
    icon = "fa-solid fa-user-lock"

    @expose("/restrictions", methods=["GET"], identity="restrictions")
    async def a_index(self, request: Request):
        search = (request.query_params.get("search") or "").strip()
        status = (request.query_params.get("status") or "active").strip() or None
        with SessionLocal() as session:
            restrictions = crud.list_restrictions(session, search=search or None, status=status)
        return await _render(
            self,
            request,
            "restrictions.html",
            restrictions=restrictions,
            search=search,
            status=status or "",
        )

    @expose("/restrictions/create", methods=["POST"], identity="restrictions-create")
    async def create(self, request: Request):
        actor = require_staff_user(request)
        form = await request.form()
        user_id = str(form.get("user_id", "")).strip()
        restriction_type = str(form.get("type", "")).strip()
        comment = str(form.get("comment", "")).strip() or None
        ends_at = _parse_optional_dt(str(form.get("ends_at", "")).strip())
        with SessionLocal() as session:
            try:
                crud.create_restriction(session, user_id, restriction_type, actor.id, ends_at, comment)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect("/admin/restrictions")

    @expose("/restrictions/{restriction_id}/revoke", methods=["POST"], identity="restrictions-revoke")
    async def revoke(self, request: Request):
        actor = require_staff_user(request)
        restriction_id = request.path_params["restriction_id"]
        form = await request.form()
        comment = str(form.get("comment", "")).strip() or None
        with SessionLocal() as session:
            try:
                crud.revoke_restriction(session, restriction_id, actor.id, comment)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect("/admin/restrictions")


class ModerationActionsView(BaseView):
    name = "Аудит"
    identity = "audit"
    icon = "fa-solid fa-clock-rotate-left"

    @expose("/audit", methods=["GET"], identity="audit")
    async def a_index(self, request: Request):
        action_type = (request.query_params.get("action_type") or "").strip() or None
        target_type = (request.query_params.get("target_type") or "").strip() or None
        moderator = (request.query_params.get("moderator") or "").strip() or None
        with SessionLocal() as session:
            actions = crud.list_moderation_actions(
                session,
                action_type=action_type,
                target_type=target_type,
                moderator_search=moderator,
            )
        return await _render(
            self,
            request,
            "audit_log.html",
            actions=actions,
            action_type=action_type or "",
            target_type=target_type or "",
            moderator=moderator or "",
        )
