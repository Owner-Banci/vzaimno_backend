from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqladmin import Admin
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from app.bootstrap import ensure_all_tables

from . import crud
from .auth import AdminAuth, require_admin_user, require_staff_user
from .db import SessionLocal, engine
from .schemas import (
    AnnouncementDecisionIn,
    ReportResolutionIn,
    RestrictionCreateIn,
    RestrictionRevokeIn,
    RoleUpdateIn,
    SupportReplyIn,
)
from .settings import get_settings
from .views_sqladmin import (
    AnnouncementsModerationView,
    ModerationActionsView,
    ReportsView,
    RestrictionsView,
    SupportThreadsView,
    UsersView,
)


settings = get_settings()
uploads_dir = Path(os.getenv("UPLOADS_DIR", "uploads"))


class StaffAdmin(Admin):
    async def index(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin/users", status_code=302)


app = FastAPI(title=settings.title)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="admin-static")
app.mount("/uploads", StaticFiles(directory=str(uploads_dir), check_dir=False), name="admin-uploads")
app.state.templates = Jinja2Templates(directory=str(settings.templates_dir))


@app.on_event("startup")
def startup() -> None:
    ensure_all_tables()


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/users", status_code=302)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


authentication_backend = AdminAuth(secret_key=settings.session_secret)
admin = StaffAdmin(
    app=app,
    engine=engine,
    authentication_backend=authentication_backend,
    base_url=settings.admin_base_url,
    title=settings.title,
    templates_dir=str(settings.templates_dir),
)
admin.add_view(UsersView)
admin.add_view(AnnouncementsModerationView)
admin.add_view(ReportsView)
admin.add_view(SupportThreadsView)
admin.add_view(RestrictionsView)
admin.add_view(ModerationActionsView)


api = APIRouter(prefix="/admin/api")


@api.get("/support/threads")
def api_support_threads(search: str | None = None, _: object = Depends(require_staff_user)):
    with SessionLocal() as session:
        return crud.list_support_threads(session, search=search)


@api.get("/support/threads/{thread_id}/messages")
def api_support_thread_messages(thread_id: str, _: object = Depends(require_staff_user)):
    with SessionLocal() as session:
        try:
            return crud.get_support_messages(session, thread_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.post("/support/threads/{thread_id}/messages")
def api_support_thread_reply(
    thread_id: str,
    payload: SupportReplyIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.post_support_message(session, thread_id, actor.id, payload.text)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/announcements/{ann_id}/decision")
def api_announcement_decision(
    ann_id: str,
    payload: AnnouncementDecisionIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            announcement = crud.apply_announcement_decision(
                session=session,
                ann_id=ann_id,
                moderator_id=actor.id,
                decision=payload.decision,
                message=payload.message,
                reasons=[reason.model_dump() for reason in payload.reasons],
                suggestions=payload.suggestions,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": announcement.id,
        "status": announcement.status,
        "deleted_at": announcement.deleted_at,
        "updated_at": announcement.updated_at,
    }


@api.post("/reports/{report_id}/resolve")
def api_resolve_report(
    report_id: str,
    payload: ReportResolutionIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            report = crud.resolve_report(
                session=session,
                report_id=report_id,
                moderator_id=actor.id,
                resolution=payload.resolution,
                moderator_comment=payload.moderator_comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": report["id"],
        "status": report["status"],
        "resolution": report["resolution"],
        "resolved_at": report["resolved_at"],
    }


@api.post("/restrictions")
def api_create_restriction(
    payload: RestrictionCreateIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            restriction = crud.create_restriction(
                session=session,
                user_id=payload.user_id,
                restriction_type=payload.type,
                moderator_id=actor.id,
                ends_at=payload.ends_at,
                comment=payload.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": restriction["id"],
        "user_id": restriction["user_id"],
        "type": restriction["type"],
        "status": restriction["status"],
    }


@api.post("/restrictions/{restriction_id}/revoke")
def api_revoke_restriction(
    restriction_id: str,
    payload: RestrictionRevokeIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            restriction = crud.revoke_restriction(
                session=session,
                restriction_id=restriction_id,
                moderator_id=actor.id,
                comment=payload.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": restriction["id"],
        "status": restriction["status"],
        "revoked_at": restriction["revoked_at"],
    }


@api.post("/users/{user_id}/role")
def api_update_user_role(
    user_id: str,
    payload: RoleUpdateIn,
    request: Request,
    _: object = Depends(require_admin_user),
):
    actor = require_admin_user(request)
    with SessionLocal() as session:
        try:
            user = crud.update_user_role(session, user_id, payload.role, actor.id, actor.role)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user


app.include_router(api)
