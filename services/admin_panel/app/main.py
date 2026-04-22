from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqladmin import Admin
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from app.bootstrap import ensure_all_tables
from app.rate_limit import check_redis_ready, redis_url
from app.runtime_hardening import apply_http_hardening, is_production_env, require_production_env_values, uploads_root

from . import crud
from .auth import (
    AdminAuth,
    StaffUser,
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
    DisputeReplyIn,
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
    DisputesView,
    ModerationActionsView,
    ReportsView,
    RestrictionsView,
    SupportThreadsView,
    UsersView,
)


settings = get_settings()
uploads_dir = uploads_root()

_PROD_REQUIRED_ENV = (
    "DATABASE_URL",
    "JWT_SECRET",
    "ADMIN_JWT_SECRET",
    "ADMIN_SESSION_SECRET",
    "REDIS_URL",
    "TRUSTED_HOSTS",
)


class StaffAdmin(Admin):
    async def index(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin/users", status_code=302)


app = FastAPI(title=settings.title)
require_production_env_values("admin", _PROD_REQUIRED_ENV)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie_name,
    same_site=settings.session_cookie_samesite,
    https_only=settings.session_cookie_secure,
    max_age=settings.session_max_age_seconds,
)
apply_http_hardening(app, service_name="admin", cors_origins_env="ADMIN_CORS_ALLOWED_ORIGINS")
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="admin-static")
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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "admin"}


def _db_ready() -> bool:
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT 1")).scalar_one()
        return bool(row == 1)
    except Exception:
        return False


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    db_ok = _db_ready()
    redis_required = bool(redis_url())
    redis_ok = True if not redis_required else bool(await check_redis_ready())
    if not db_ok or not redis_ok:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "db": db_ok,
                "redis": redis_ok,
                "redis_required": redis_required,
            },
        )
    return {
        "status": "ready",
        "service": "admin",
        "db": True,
        "redis": redis_ok,
        "production_mode": is_production_env(),
    }


@app.get("/uploads/{ann_id}/{filename}")
def admin_download_upload(
    ann_id: str,
    filename: str,
    _: object = Depends(require_staff_user),
) -> FileResponse:
    for field_name, value in (("ann_id", ann_id), ("filename", filename)):
        if "/" in value or "\\" in value or ".." in value:
            raise HTTPException(status_code=400, detail=f"Invalid {field_name}")

    file_path = (uploads_dir / ann_id / filename).resolve()
    try:
        file_path.relative_to(uploads_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


api = APIRouter(prefix="/admin/api")


@api.post("/auth/login", response_model=AdminTokenOut)
async def api_admin_login(payload: AdminLoginIn, request: Request):
    user, access_token, _refresh_token = await authenticate_admin_credentials(
        payload.login_identifier,
        payload.password,
        request,
    )
    return AdminTokenOut(
        access_token=access_token,
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
def api_admin_me(actor: StaffUser = Depends(require_staff_user)):
    return {
        "id": actor.id,
        "login_identifier": actor.login_identifier,
        "email": actor.email,
        "role": actor.role,
        "display_name": actor.display_name,
        "linked_user_account_id": actor.linked_user_account_id,
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


@api.get("/disputes")
def api_disputes(
    request: Request,
    search: str | None = None,
    state: str | None = None,
    _: object = Depends(require_staff_user),
):
    require_staff_user(request)
    with SessionLocal() as session:
        try:
            return {
                "items": crud.list_disputes(session, search=search, moderation_state=state),
                "pending_count": crud.count_pending_disputes(session),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.get("/disputes/{dispute_id}")
def api_dispute_detail(
    dispute_id: str,
    request: Request,
    _: object = Depends(require_staff_user),
):
    require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.get_dispute(session, dispute_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.get("/disputes/{dispute_id}/messages")
def api_dispute_messages(
    dispute_id: str,
    request: Request,
    limit: int = 200,
    _: object = Depends(require_staff_user),
):
    require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.get_dispute_messages(session, dispute_id, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.post("/disputes/{dispute_id}/join")
def api_join_dispute(
    dispute_id: str,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.join_dispute(session, dispute_id, actor.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@api.post("/disputes/{dispute_id}/messages")
def api_post_dispute_message(
    dispute_id: str,
    payload: DisputeReplyIn,
    request: Request,
    _: object = Depends(require_staff_user),
):
    actor = require_staff_user(request)
    with SessionLocal() as session:
        try:
            return crud.post_dispute_message(session, dispute_id, actor.id, payload.text)
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
admin.add_view(DisputesView)
admin.add_view(SupportThreadsView)
admin.add_view(RestrictionsView)
admin.add_view(ModerationActionsView)
