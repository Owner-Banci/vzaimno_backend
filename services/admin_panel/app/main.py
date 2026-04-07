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
from .auth import (
    AdminAuth,
    authenticate_admin_credentials,
    require_admin_user,
    require_staff_user,
    revoke_admin_session,
)
from .db import SessionLocal, engine
from .schemas import (
    AdminAccessCreateIn,
    AdminAccessResetIn,
    AdminLoginIn,
    AdminTokenOut,
    AnnouncementDecisionIn,
    ReportResolutionIn,
    RestrictionCreateIn,
    RestrictionExtendIn,
    RestrictionRevokeIn,
    SupportAssignmentIn,
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


api = APIRouter(prefix="/admin/api")


@api.post("/auth/login", response_model=AdminTokenOut)
def api_admin_login(payload: AdminLoginIn, request: Request):
    user, token = authenticate_admin_credentials(payload.login_identifier, payload.password, request)
    return AdminTokenOut(
        access_token=token,
        admin_account_id=user.id,
        role=user.role,
        display_name=user.display_name,
    )


@api.post("/auth/logout")
def api_admin_logout(request: Request, _: object = Depends(require_staff_user)):
    revoke_admin_session(request.session.get("admin_session_id"))
    request.session.clear()
    return {"ok": True}


@api.get("/auth/me")
def api_admin_me(request: Request, actor: object = Depends(require_staff_user)):
    user = require_staff_user(request)
    return {
        "id": user.id,
        "login_identifier": user.login_identifier,
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "linked_user_account_id": user.linked_user_account_id,
    }


@api.get("/support/threads")
def api_support_threads(
    request: Request,
    search: str | None = None,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        return crud.list_support_threads(session, actor.id, search=search)


@api.get("/support/threads/{thread_id}/messages")
def api_support_thread_messages(
    thread_id: str,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.get_support_messages(session, thread_id, actor.id)
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


@api.post("/support/threads/{thread_id}/assign")
def api_support_thread_assign(
    thread_id: str,
    payload: SupportAssignmentIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.assign_support_thread(session, thread_id, payload.assigned_admin_account_id, actor.id)
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
        "id": announcement["id"],
        "status": announcement["status"],
        "deleted_at": announcement["deleted_at"],
        "updated_at": announcement["updated_at"],
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
                ends_at=payload.ends_at,
                custom_restriction_label=payload.custom_restriction_label,
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
                source_type=payload.source_type,
                source_id=payload.source_id,
                meta={"custom_label": payload.custom_label} if payload.custom_label else None,
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


@api.post("/restrictions/{restriction_id}/extend")
def api_extend_restriction(
    restriction_id: str,
    payload: RestrictionExtendIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            restriction = crud.extend_restriction(
                session=session,
                restriction_id=restriction_id,
                moderator_id=actor.id,
                ends_at=payload.ends_at,
                comment=payload.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": restriction["id"],
        "status": restriction["status"],
        "ends_at": restriction["ends_at"],
    }


@api.get("/users/{user_id}/admin-access")
def api_user_admin_access(
    user_id: str,
    _: object = Depends(require_admin_user),
):
    with SessionLocal() as session:
        try:
            return crud.get_user_admin_access(session, user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/users/{user_id}/admin-access")
def api_create_user_admin_access(
    user_id: str,
    payload: AdminAccessCreateIn,
    request: Request,
    _: object = Depends(require_admin_user),
):
    actor = require_admin_user(request)
    with SessionLocal() as session:
        try:
            return crud.create_admin_access_for_user(
                session=session,
                user_id=user_id,
                login_identifier=payload.login_identifier,
                display_name=payload.display_name,
                role=payload.role,
                password=payload.password,
                email=str(payload.email) if payload.email else None,
                actor_admin_account_id=actor.id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.get("/admin-accounts/active")
def api_active_admin_accounts(_: object = Depends(require_staff_user)):
    with SessionLocal() as session:
        return crud.list_active_admin_accounts(session)


@api.post("/admin-accounts/{admin_account_id}/disable")
def api_disable_admin_account(
    admin_account_id: str,
    request: Request,
    _: object = Depends(require_admin_user),
):
    actor = require_admin_user(request)
    with SessionLocal() as session:
        try:
            return crud.disable_admin_account(session, admin_account_id, actor.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/admin-accounts/{admin_account_id}/enable")
def api_enable_admin_account(
    admin_account_id: str,
    request: Request,
    _: object = Depends(require_admin_user),
):
    actor = require_admin_user(request)
    with SessionLocal() as session:
        try:
            return crud.enable_admin_account(session, admin_account_id, actor.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/admin-accounts/{admin_account_id}/reset-credentials")
def api_reset_admin_credentials(
    admin_account_id: str,
    payload: AdminAccessResetIn,
    request: Request,
    _: object = Depends(require_admin_user),
):
    actor = require_admin_user(request)
    with SessionLocal() as session:
        try:
            return crud.reset_admin_account_credentials(session, admin_account_id, payload.password, actor.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


app.include_router(api)

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
