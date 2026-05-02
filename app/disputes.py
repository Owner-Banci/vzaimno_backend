from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException

from app.chat import post_system_thread_message
from app.config import get_env, get_float, get_int
from app.db import execute, fetch_all, fetch_one
from app.external import call_external_sync
from app.logging_utils import logger
from app.user_identity import user_display_name_sql


DEFAULT_DISPUTE_GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_DISPUTE_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DISPUTE_GROQ_MODEL = get_env("DISPUTE_GROQ_MODEL", DEFAULT_DISPUTE_GROQ_MODEL) or DEFAULT_DISPUTE_GROQ_MODEL
DISPUTE_MODEL_PROVIDER = f"groq:{DISPUTE_GROQ_MODEL}"
DISPUTE_RESPONSE_TIMEOUT_SECONDS = max(3.0, get_float("DISPUTE_GROQ_TIMEOUT_S", 25.0))
DISPUTE_GROQ_RETRIES = max(0, get_int("DISPUTE_GROQ_RETRIES", 2))
DISPUTE_GROQ_BASE_URL = (
    get_env("DISPUTE_GROQ_BASE_URL", DEFAULT_DISPUTE_GROQ_BASE_URL)
    or DEFAULT_DISPUTE_GROQ_BASE_URL
)
DISPUTE_GROQ_API_KEY = get_env("DISPUTE_GROQ_API_KEY", "") or ""

DISPUTE_STATUS_WAITING_COUNTERPARTY = "open_waiting_counterparty"
DISPUTE_STATUS_MODEL_THINKING = "model_thinking"
DISPUTE_STATUS_WAITING_CLARIFICATIONS = "waiting_clarification_answers"
DISPUTE_STATUS_WAITING_ROUND_1_VOTES = "waiting_round_1_votes"
DISPUTE_STATUS_WAITING_ROUND_2_VOTES = "waiting_round_2_votes"
DISPUTE_STATUS_CLOSED_BY_ACCEPTANCE = "closed_by_acceptance"
DISPUTE_STATUS_RESOLVED = "resolved"
DISPUTE_STATUS_AWAITING_MODERATOR = "awaiting_moderator"

DISPUTE_ACTIVE_STATUSES = {
    DISPUTE_STATUS_WAITING_COUNTERPARTY,
    DISPUTE_STATUS_MODEL_THINKING,
    DISPUTE_STATUS_WAITING_CLARIFICATIONS,
    DISPUTE_STATUS_WAITING_ROUND_1_VOTES,
    DISPUTE_STATUS_WAITING_ROUND_2_VOTES,
    DISPUTE_STATUS_AWAITING_MODERATOR,
}

SYSTEM_PROMPT_FALLBACK = """
Ты специализированный медиатор только по спорам маркетплейса услуг.

Ключевые правила:
1) Работаешь только с переданным JSON-контекстом. Никаких выдуманных фактов.
2) Перед ответом оцени достаточность данных по критериям: факты выполнения, исходные условия задания, спорные суммы/условия, подтверждения из чата.
3) Если есть неопределённость по любому критичному пункту — верни response_type="questions" и задай 1..5 вопросов.
4) Вопросы должны быть максимально предметными и привязанными к конкретному спорному месту из контекста чата или задания.
5) Если данных достаточно — верни 3 структурированных варианта урегулирования.
6) Не обсуждай виновность в юридическом смысле. Цель — практическое урегулирование.
7) Варианты должны быть реалистичными: частичный/полный возврат, возврат с товаром, переделка, частичное урегулирование, предупреждение.
8) Все поля ответа — только на русском языке.
9) Формат ответа строго JSON, без markdown и без дополнительных пояснений.
10) Для раунда 1 верни response_type="settlement_options_round_1".
11) Для раунда 2 верни response_type="settlement_options_round_2".
12) Во втором раунде обязательно смещай варианты к компромиссу: крайние варианты должны быть менее выгодны соответствующим сторонам, чем в раунде 1.
13) Не возвращай reasoning цепочку рассуждений, только структурированный итог.
14) В description, customer_action и performer_action давай убедительное объяснение "почему этот вариант подходит именно в этих обстоятельствах", с опорой на:
   - детали задания;
   - факты/формулировки из чата;
   - ответы сторон в анкетах;
   - (если есть) уточняющие ответы.
15) Не используй общие фразы вида "это компромисс". Всегда указывай конкретную пользу и риск при отказе.
16) customer_action и performer_action должны быть разными и персонализированными под роль.

JSON contract:
{
  "response_type": "questions | settlement_options_round_1 | settlement_options_round_2",
  "summary": "string",
  "questions": [
    {
      "id": "q1",
      "addressed_party": "both | initiator | counterparty",
      "text": "string"
    }
  ],
  "settlement_options": [
    {
      "id": "opt_1",
      "lean": "initiator_favor | counterparty_favor | compromise",
      "title": "string",
      "description": "string",
      "customer_action": "string",
      "performer_action": "string",
      "compensation_rub": 0,
      "refund_percent": 0,
      "resolution_kind": "partial_refund | full_refund | return_and_refund | redo | warning_only | other"
    }
  ]
}
""".strip()


_MODEL_INFLIGHT_LOCK = threading.Lock()
_MODEL_INFLIGHT: Set[str] = set()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: Any, *, max_len: int = 4000) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > max_len:
        return text[:max_len]
    return text


def _normalize_long_text(value: Any, *, max_len: int = 12000) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        return text[:max_len]
    return text


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
    return []


def _party_role_for_user(dispute_row: Dict[str, Any], user_id: str) -> Optional[str]:
    if dispute_row["initiator_user_id"] == user_id:
        return dispute_row["initiator_party_role"]
    if dispute_row["counterparty_user_id"] == user_id:
        return "performer" if dispute_row["initiator_party_role"] == "customer" else "customer"
    return None


def _viewer_side(dispute_row: Dict[str, Any], user_id: str) -> str:
    if dispute_row["initiator_user_id"] == user_id:
        return "initiator"
    if dispute_row["counterparty_user_id"] == user_id:
        return "counterparty"
    return "none"


def _thread_parties(thread_id: str) -> Tuple[str, str]:
    rows = fetch_all(
        """
        SELECT user_id::text, role
        FROM chat_participants
        WHERE thread_id::text = %s
          AND left_at IS NULL
        ORDER BY joined_at ASC
        """,
        (thread_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Thread not found")

    customer_id: Optional[str] = None
    performer_id: Optional[str] = None
    fallback: List[str] = []

    for row in rows:
        user_id = str(row[0])
        role = str(row[1] or "").lower()
        if user_id not in fallback:
            fallback.append(user_id)

        if role in {"owner", "customer"} and customer_id is None:
            customer_id = user_id
        elif role == "performer" and performer_id is None:
            performer_id = user_id

    if customer_id and performer_id:
        return customer_id, performer_id

    if len(fallback) < 2:
        raise HTTPException(status_code=400, detail="Dispute requires at least two chat participants")

    if not customer_id:
        customer_id = fallback[0]
    if not performer_id:
        performer_id = fallback[1] if fallback[1] != customer_id else (fallback[2] if len(fallback) > 2 else None)

    if not performer_id or performer_id == customer_id:
        raise HTTPException(status_code=400, detail="Unable to determine dispute parties")

    return customer_id, performer_id


def _user_display_name(user_id: str) -> str:
    display_name_sql, display_name_params = user_display_name_sql(
        user_alias="u",
        profile_alias="up",
        fallback="Пользователь",
    )
    row = fetch_one(
        f"""
        SELECT {display_name_sql}
        FROM users u
        LEFT JOIN user_profiles up ON up.user_id = u.id
        WHERE u.id::text = %s
        LIMIT 1
        """,
        (*display_name_params, user_id),
    )
    return str(row[0]) if row and row[0] else "Пользователь"


def _dispute_select_sql() -> str:
    return """
        SELECT
            d.id::text,
            d.thread_id::text,
            d.status,
            d.initiator_user_id::text,
            d.counterparty_user_id::text,
            d.initiator_party_role,
            d.opened_by_display_name,
            d.initiator_form,
            d.counterparty_form,
            d.counterparty_deadline_at,
            d.active_round,
            d.clarifying_questions,
            d.clarification_answers,
            d.round1_options,
            d.round2_options,
            d.round1_votes,
            d.round2_votes,
            d.resolution_summary,
            d.selected_option_id,
            d.moderator_hook,
            d.last_model_error,
            d.model_attempts,
            d.created_at,
            d.updated_at,
            d.closed_at
        FROM disputes d
    """


def _row_to_dispute_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "thread_id": str(row[1]),
        "status": str(row[2]),
        "initiator_user_id": str(row[3]),
        "counterparty_user_id": str(row[4]),
        "initiator_party_role": str(row[5]),
        "opened_by_display_name": str(row[6] or "Пользователь"),
        "initiator_form": _as_dict(row[7]),
        "counterparty_form": _as_dict(row[8]),
        "counterparty_deadline_at": row[9],
        "active_round": int(row[10] or 1),
        "clarifying_questions": _as_list(row[11]),
        "clarification_answers": _as_dict(row[12]),
        "round1_options": _as_list(row[13]),
        "round2_options": _as_list(row[14]),
        "round1_votes": _as_dict(row[15]),
        "round2_votes": _as_dict(row[16]),
        "resolution_summary": row[17],
        "selected_option_id": row[18],
        "moderator_hook": _as_dict(row[19]),
        "last_model_error": row[20],
        "model_attempts": int(row[21] or 0),
        "created_at": row[22],
        "updated_at": row[23],
        "closed_at": row[24],
    }


def _active_dispute_status_sql_list() -> str:
    return ",".join(f"'{status}'" for status in sorted(DISPUTE_ACTIVE_STATUSES))


def _fetch_active_dispute_row(thread_id: str) -> Optional[Dict[str, Any]]:
    row = fetch_one(
        _dispute_select_sql()
        + f"""
        WHERE d.thread_id::text = %s
          AND d.status IN ({_active_dispute_status_sql_list()})
        ORDER BY d.created_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )
    if not row:
        return None
    return _row_to_dispute_dict(row)


def _fetch_dispute_row_by_id(thread_id: str, dispute_id: str) -> Optional[Dict[str, Any]]:
    row = fetch_one(
        _dispute_select_sql()
        + """
        WHERE d.thread_id::text = %s
          AND d.id::text = %s
        LIMIT 1
        """,
        (thread_id, dispute_id),
    )
    if not row:
        return None
    return _row_to_dispute_dict(row)


def _insert_dispute_event(dispute_id: str, event_type: str, actor_user_id: Optional[str], payload: Dict[str, Any]) -> None:
    execute(
        """
        INSERT INTO dispute_events (id, dispute_id, event_type, actor_user_id, payload, created_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, now())
        """,
        (str(uuid.uuid4()), dispute_id, event_type, actor_user_id, json.dumps(payload, ensure_ascii=False)),
    )


def _safe_post_system_message(thread_id: str, text: str) -> None:
    try:
        post_system_thread_message(thread_id, text)
    except Exception:
        logger.warning(
            "dispute_system_message_failed",
            extra={"thread_id": thread_id, "status_code": 0},
        )


def _required_answer_roles(dispute: Dict[str, Any]) -> Set[str]:
    questions = dispute.get("clarifying_questions") or []
    required: Set[str] = set()

    initiator_party = dispute["initiator_party_role"]
    counterparty_party = "performer" if initiator_party == "customer" else "customer"

    for question in questions:
        if not isinstance(question, dict):
            continue
        addressed = str(question.get("addressed_party") or "both").strip().lower()
        if addressed == "initiator":
            required.add(initiator_party)
        elif addressed == "counterparty":
            required.add(counterparty_party)
        else:
            required.update({"customer", "performer"})

    if not required:
        required.update({"customer", "performer"})

    return required


def _active_options(dispute: Dict[str, Any]) -> List[Dict[str, Any]]:
    if dispute["active_round"] == 2:
        return [item for item in dispute.get("round2_options") or [] if isinstance(item, dict)]
    return [item for item in dispute.get("round1_options") or [] if isinstance(item, dict)]


def _active_votes(dispute: Dict[str, Any]) -> Dict[str, str]:
    if dispute["active_round"] == 2:
        raw = dispute.get("round2_votes") or {}
    else:
        raw = dispute.get("round1_votes") or {}
    result: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            k = str(key)
            v = _normalize_text(value, max_len=128)
            if k in {"customer", "performer"} and v:
                result[k] = v
    return result


def _build_dispute_state_out(dispute: Dict[str, Any], viewer_user_id: str) -> Dict[str, Any]:
    viewer_side = _viewer_side(dispute, viewer_user_id)
    viewer_party_role = _party_role_for_user(dispute, viewer_user_id)

    questions = [item for item in dispute.get("clarifying_questions") or [] if isinstance(item, dict)]
    answers = dispute.get("clarification_answers") or {}
    required_roles = sorted(_required_answer_roles(dispute)) if dispute["status"] == DISPUTE_STATUS_WAITING_CLARIFICATIONS else []

    options = _active_options(dispute)
    votes = _active_votes(dispute)
    my_vote = votes.get(viewer_party_role or "", None)

    initiator_terms = {
        "requested_compensation_rub": int(dispute["initiator_form"].get("requested_compensation_rub") or 0),
        "desired_resolution": _normalize_text(dispute["initiator_form"].get("desired_resolution") or "", max_len=64),
        "problem_title": _normalize_text(dispute["initiator_form"].get("problem_title") or "", max_len=120),
    }

    return {
        "id": dispute["id"],
        "thread_id": dispute["thread_id"],
        "status": dispute["status"],
        "initiator_user_id": dispute["initiator_user_id"],
        "counterparty_user_id": dispute["counterparty_user_id"],
        "initiator_party_role": dispute["initiator_party_role"],
        "viewer_side": viewer_side,
        "viewer_party_role": viewer_party_role,
        "opened_by_display_name": dispute["opened_by_display_name"],
        "counterparty_deadline_at": dispute.get("counterparty_deadline_at"),
        "active_round": int(dispute.get("active_round") or 1),
        "is_model_thinking": dispute["status"] == DISPUTE_STATUS_MODEL_THINKING,
        "resolution_summary": dispute.get("resolution_summary"),
        "selected_option_id": dispute.get("selected_option_id"),
        "moderator_required": dispute["status"] == DISPUTE_STATUS_AWAITING_MODERATOR,
        "questions": questions,
        "required_answer_party_roles": required_roles,
        "options": options,
        "votes": votes,
        "my_vote_option_id": my_vote,
        "initiator_terms": initiator_terms,
        "last_model_error": dispute.get("last_model_error"),
    }


def _transition_to_moderator_timeout(dispute: Dict[str, Any]) -> Dict[str, Any]:
    execute(
        """
        UPDATE disputes
        SET status = %s,
            moderator_hook = %s::jsonb,
            resolution_summary = %s,
            closed_at = now(),
            updated_at = now()
        WHERE id::text = %s
        """,
        (
            DISPUTE_STATUS_AWAITING_MODERATOR,
            json.dumps({"status": "pending", "reason": "counterparty_timeout"}, ensure_ascii=False),
            "Вторая сторона не ответила в течение 48 часов. Спор ожидает модератора.",
            dispute["id"],
        ),
    )
    _insert_dispute_event(dispute["id"], "counterparty_timeout", None, {"thread_id": dispute["thread_id"]})
    _safe_post_system_message(
        dispute["thread_id"],
        "48 часов на ответ истекли. Автоматическая часть спора завершена, спор ожидает подключения модератора.",
    )
    updated = _fetch_dispute_row_by_id(dispute["thread_id"], dispute["id"])
    return updated or dispute


def _apply_counterparty_timeout_if_needed(dispute: Dict[str, Any]) -> Dict[str, Any]:
    if dispute["status"] != DISPUTE_STATUS_WAITING_COUNTERPARTY:
        return dispute

    deadline = dispute.get("counterparty_deadline_at")
    if not isinstance(deadline, datetime):
        return dispute

    if _now_utc() <= deadline:
        return dispute

    return _transition_to_moderator_timeout(dispute)


def get_active_dispute_state(thread_id: str, viewer_user_id: str) -> Optional[Dict[str, Any]]:
    dispute = _fetch_active_dispute_row(thread_id)
    if not dispute:
        return None

    dispute = _apply_counterparty_timeout_if_needed(dispute)
    return _build_dispute_state_out(dispute, viewer_user_id)


def open_dispute(
    *,
    thread_id: str,
    actor_user_id: str,
    problem_title: str,
    problem_description: str,
    requested_compensation_rub: int,
    desired_resolution: str,
) -> Dict[str, Any]:
    existing = _fetch_active_dispute_row(thread_id)
    if existing:
        raise HTTPException(status_code=409, detail="Active dispute already exists for this chat")

    customer_id, performer_id = _thread_parties(thread_id)
    if actor_user_id not in {customer_id, performer_id}:
        raise HTTPException(status_code=403, detail="Only chat participants can open dispute")

    initiator_party_role = "customer" if actor_user_id == customer_id else "performer"
    counterparty_user_id = performer_id if actor_user_id == customer_id else customer_id

    title = _normalize_text(problem_title, max_len=120)
    description = _normalize_long_text(problem_description, max_len=5000)
    desired = _normalize_text(desired_resolution, max_len=64)

    if not description:
        raise HTTPException(status_code=400, detail="Problem description is required")

    if requested_compensation_rub < 0:
        raise HTTPException(status_code=400, detail="Requested compensation must be non-negative")

    dispute_id = str(uuid.uuid4())
    opened_by_name = _user_display_name(actor_user_id)
    deadline_at = _now_utc() + timedelta(hours=48)

    initiator_form = {
        "problem_title": title,
        "problem_description": description,
        "requested_compensation_rub": int(requested_compensation_rub),
        "desired_resolution": desired,
    }

    execute(
        """
        INSERT INTO disputes (
            id,
            thread_id,
            status,
            initiator_user_id,
            counterparty_user_id,
            initiator_party_role,
            opened_by_display_name,
            initiator_form,
            counterparty_deadline_at,
            active_round,
            model_provider,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 1, %s, now(), now())
        """,
        (
            dispute_id,
            thread_id,
            DISPUTE_STATUS_WAITING_COUNTERPARTY,
            actor_user_id,
            counterparty_user_id,
            initiator_party_role,
            opened_by_name,
            json.dumps(initiator_form, ensure_ascii=False),
            deadline_at,
            DISPUTE_MODEL_PROVIDER,
        ),
    )
    _insert_dispute_event(
        dispute_id,
        "opened",
        actor_user_id,
        {
            "initiator_party_role": initiator_party_role,
            "counterparty_user_id": counterparty_user_id,
            "counterparty_deadline_at": deadline_at.isoformat(),
        },
    )

    _safe_post_system_message(
        thread_id,
        f"{opened_by_name} открыл(а) спор. Вторая сторона может согласиться с условиями или отправить свою анкету. Срок ответа: 48 часов.",
    )

    created = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create dispute")

    return _build_dispute_state_out(created, actor_user_id)


def counterparty_accept(
    *,
    thread_id: str,
    dispute_id: str,
    actor_user_id: str,
) -> Dict[str, Any]:
    dispute = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    dispute = _apply_counterparty_timeout_if_needed(dispute)

    if dispute["status"] == DISPUTE_STATUS_CLOSED_BY_ACCEPTANCE:
        return _build_dispute_state_out(dispute, actor_user_id)

    if dispute["status"] != DISPUTE_STATUS_WAITING_COUNTERPARTY:
        raise HTTPException(status_code=409, detail="Dispute is no longer waiting for counterparty response")

    if actor_user_id != dispute["counterparty_user_id"]:
        raise HTTPException(status_code=403, detail="Only counterparty can accept initiator terms")

    execute(
        """
        UPDATE disputes
        SET status = %s,
            resolution_summary = %s,
            closed_at = now(),
            updated_at = now()
        WHERE id::text = %s
        """,
        (
            DISPUTE_STATUS_CLOSED_BY_ACCEPTANCE,
            "Вторая сторона полностью согласилась с условиями инициатора. Спор закрыт без участия модели.",
            dispute_id,
        ),
    )
    _insert_dispute_event(dispute_id, "counterparty_accepted", actor_user_id, {})
    _safe_post_system_message(
        thread_id,
        "Вторая сторона полностью согласилась с условиями инициатора. Спор закрыт без участия модели.",
    )

    updated = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Dispute update failed")
    return _build_dispute_state_out(updated, actor_user_id)


def counterparty_submit_form(
    *,
    thread_id: str,
    dispute_id: str,
    actor_user_id: str,
    response_description: str,
    acceptable_refund_percent: int,
    desired_resolution: str,
) -> Tuple[Dict[str, Any], bool]:
    dispute = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    dispute = _apply_counterparty_timeout_if_needed(dispute)

    if dispute["status"] != DISPUTE_STATUS_WAITING_COUNTERPARTY:
        raise HTTPException(status_code=409, detail="Dispute is no longer waiting for counterparty response")

    if actor_user_id != dispute["counterparty_user_id"]:
        raise HTTPException(status_code=403, detail="Only counterparty can submit response")

    if acceptable_refund_percent < 0 or acceptable_refund_percent > 100:
        raise HTTPException(status_code=400, detail="acceptable_refund_percent must be in 0..100")

    response_text = _normalize_long_text(response_description, max_len=5000)
    desired = _normalize_text(desired_resolution, max_len=64)
    if not response_text:
        raise HTTPException(status_code=400, detail="Response description is required")

    counterparty_form = {
        "response_description": response_text,
        "acceptable_refund_percent": int(acceptable_refund_percent),
        "desired_resolution": desired,
    }

    execute(
        """
        UPDATE disputes
        SET counterparty_form = %s::jsonb,
            status = %s,
            active_round = 1,
            clarifying_questions = '[]'::jsonb,
            clarification_answers = '{}'::jsonb,
            round1_votes = '{}'::jsonb,
            round2_votes = '{}'::jsonb,
            updated_at = now()
        WHERE id::text = %s
        """,
        (json.dumps(counterparty_form, ensure_ascii=False), DISPUTE_STATUS_MODEL_THINKING, dispute_id),
    )

    _insert_dispute_event(dispute_id, "counterparty_form_submitted", actor_user_id, counterparty_form)
    _safe_post_system_message(thread_id, "Встречная анкета получена. Модель анализирует спор…")

    updated = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Dispute update failed")

    return _build_dispute_state_out(updated, actor_user_id), True


def _strip_markdown_fences(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return raw


def _normalize_questions(raw_items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(_as_list(raw_items), start=1):
        if not isinstance(item, dict):
            continue
        text = _normalize_long_text(item.get("text"), max_len=500)
        if not text:
            continue
        addressed = _normalize_text(item.get("addressed_party") or "both", max_len=32).lower()
        if addressed not in {"both", "initiator", "counterparty"}:
            addressed = "both"
        question_id = _normalize_text(item.get("id") or f"q{idx}", max_len=32)
        out.append({"id": question_id, "addressed_party": addressed, "text": text})
    return out[:5]


def _normalize_option(item: Dict[str, Any], *, fallback_id: str, order_amount: int) -> Dict[str, Any]:
    option_id = _normalize_text(item.get("id") or fallback_id, max_len=64)
    lean = _normalize_text(item.get("lean") or "compromise", max_len=32).lower()
    if lean not in {"initiator_favor", "counterparty_favor", "compromise"}:
        lean = "compromise"

    title = _normalize_text(item.get("title") or "Вариант урегулирования", max_len=120)
    description = _normalize_long_text(item.get("description") or "", max_len=1400)
    customer_action = _normalize_long_text(item.get("customer_action") or "Подтвердить вариант", max_len=1100)
    performer_action = _normalize_long_text(item.get("performer_action") or "Подтвердить вариант", max_len=1100)

    compensation_raw = item.get("compensation_rub")
    compensation: Optional[int]
    try:
        compensation = int(compensation_raw) if compensation_raw is not None else None
    except Exception:
        compensation = None
    if compensation is not None:
        compensation = max(0, min(compensation, max(0, order_amount)))

    refund_raw = item.get("refund_percent")
    refund_percent: Optional[int]
    try:
        refund_percent = int(refund_raw) if refund_raw is not None else None
    except Exception:
        refund_percent = None
    if refund_percent is not None:
        refund_percent = max(0, min(refund_percent, 100))

    resolution_kind = _normalize_text(item.get("resolution_kind") or "other", max_len=64).lower()
    if resolution_kind not in {
        "partial_refund",
        "full_refund",
        "return_and_refund",
        "redo",
        "warning_only",
        "other",
    }:
        resolution_kind = "other"

    return {
        "id": option_id,
        "lean": lean,
        "title": title,
        "description": description,
        "customer_action": customer_action,
        "performer_action": performer_action,
        "compensation_rub": compensation,
        "refund_percent": refund_percent,
        "resolution_kind": resolution_kind,
    }


def _normalize_options(raw_items: Any, *, order_amount: int) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for index, item in enumerate(_as_list(raw_items), start=1):
        if not isinstance(item, dict):
            continue
        options.append(_normalize_option(item, fallback_id=f"opt_{index}", order_amount=order_amount))
    return options[:3]


def _fallback_questions() -> List[Dict[str, Any]]:
    return [
        {
            "id": "q1",
            "addressed_party": "both",
            "text": "Что именно стороны согласовали до начала выполнения и в какой форме это было зафиксировано?",
        },
        {
            "id": "q2",
            "addressed_party": "initiator",
            "text": "Какая часть результата не соответствует ожиданиям и какой фактический ущерб вы понесли?",
        },
        {
            "id": "q3",
            "addressed_party": "counterparty",
            "text": "Что помешало выполнить задачу полностью и какой вариант урегулирования вы считаете справедливым?",
        },
    ]


def _compensation_from_percent(order_amount: int, percent: int) -> int:
    if order_amount <= 0:
        return 0
    return max(0, min(order_amount, int(round((order_amount * max(0, min(percent, 100))) / 100.0))))


def _fallback_options_round_1(dispute: Dict[str, Any], *, summary: str) -> List[Dict[str, Any]]:
    order_amount = max(0, int(dispute["initiator_form"].get("requested_compensation_rub") or 0))
    # Если инициатор запросил 0, пробуем взять ориентир по чату/контексту.
    if order_amount == 0:
        order_amount = 100

    requested = max(0, int(dispute["initiator_form"].get("requested_compensation_rub") or order_amount))
    acceptable_percent = max(0, min(100, int(dispute["counterparty_form"].get("acceptable_refund_percent") or 0)))
    acceptable = _compensation_from_percent(order_amount, acceptable_percent)

    high = max(requested, acceptable)
    low = min(requested, acceptable)
    compromise = int(round((high + low) / 2.0)) if high or low else int(round(order_amount * 0.45))

    return [
        {
            "id": "r1_opt_1",
            "lean": "initiator_favor",
            "title": "Более выгодно инициатору",
            "description": "Увеличенная компенсация в пользу стороны, открывшей спор.",
            "customer_action": f"Подтвердить компенсацию {high} ₽ и закрыть спор.",
            "performer_action": f"Выплатить {high} ₽ и завершить урегулирование.",
            "compensation_rub": high,
            "refund_percent": int(round((high / max(order_amount, 1)) * 100)),
            "resolution_kind": "partial_refund",
        },
        {
            "id": "r1_opt_2",
            "lean": "counterparty_favor",
            "title": "Более выгодно второй стороне",
            "description": "Смягчённый объём компенсации с более мягкими условиями.",
            "customer_action": f"Принять {low} ₽ как финальное урегулирование.",
            "performer_action": f"Выплатить {low} ₽ и закрыть спор.",
            "compensation_rub": low,
            "refund_percent": int(round((low / max(order_amount, 1)) * 100)),
            "resolution_kind": "partial_refund",
        },
        {
            "id": "r1_opt_3",
            "lean": "compromise",
            "title": "Компромиссный вариант",
            "description": f"Серединное решение для завершения спора без эскалации. {summary}",
            "customer_action": f"Принять {compromise} ₽ и зафиксировать закрытие кейса.",
            "performer_action": f"Выплатить {compromise} ₽ и закрыть спор.",
            "compensation_rub": compromise,
            "refund_percent": int(round((compromise / max(order_amount, 1)) * 100)),
            "resolution_kind": "partial_refund",
        },
    ]


def _fallback_options_round_2(dispute: Dict[str, Any], *, summary: str) -> List[Dict[str, Any]]:
    round1_options = [item for item in dispute.get("round1_options") or [] if isinstance(item, dict)]
    if not round1_options:
        return _fallback_options_round_1(dispute, summary=summary)

    compensated = [item.get("compensation_rub") for item in round1_options if isinstance(item.get("compensation_rub"), int)]
    if compensated:
        min_v = min(compensated)
        max_v = max(compensated)
        mid = int(round((min_v + max_v) / 2.0))
    else:
        min_v, max_v, mid = 0, 100, 50

    tighter_min = int(round(mid - (mid - min_v) * 0.6))
    tighter_max = int(round(mid + (max_v - mid) * 0.6))

    return [
        {
            "id": "r2_opt_1",
            "lean": "initiator_favor",
            "title": "Обновлённый вариант для инициатора",
            "description": "Менее крайняя версия по сравнению с первым раундом.",
            "customer_action": f"Принять {tighter_max} ₽ и закрыть спор.",
            "performer_action": f"Выплатить {tighter_max} ₽ и завершить урегулирование.",
            "compensation_rub": tighter_max,
            "refund_percent": None,
            "resolution_kind": "partial_refund",
        },
        {
            "id": "r2_opt_2",
            "lean": "counterparty_favor",
            "title": "Обновлённый вариант для второй стороны",
            "description": "Более компромиссная версия по сравнению с первым раундом.",
            "customer_action": f"Принять {tighter_min} ₽ как итог второго раунда.",
            "performer_action": f"Выплатить {tighter_min} ₽ и закрыть спор.",
            "compensation_rub": tighter_min,
            "refund_percent": None,
            "resolution_kind": "partial_refund",
        },
        {
            "id": "r2_opt_3",
            "lean": "compromise",
            "title": "Усиленный компромисс",
            "description": f"Раунд 2: вариант для финального сближения позиций. {summary}",
            "customer_action": f"Принять {mid} ₽ и завершить спор без эскалации.",
            "performer_action": f"Выплатить {mid} ₽ и закрыть спор.",
            "compensation_rub": mid,
            "refund_percent": None,
            "resolution_kind": "partial_refund",
        },
    ]


def _find_option_by_id(options: List[Dict[str, Any]], option_id: str) -> Optional[Dict[str, Any]]:
    for item in options:
        if _normalize_text(item.get("id"), max_len=64) == option_id:
            return item
    return None


def _enforce_round2_compromise(dispute: Dict[str, Any], options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    round1_options = [item for item in dispute.get("round1_options") or [] if isinstance(item, dict)]
    round1_votes = dispute.get("round1_votes") or {}

    if not round1_options or not isinstance(round1_votes, dict):
        return options

    initiator_party = dispute["initiator_party_role"]
    counterparty_party = "performer" if initiator_party == "customer" else "customer"

    initiator_choice = _find_option_by_id(round1_options, _normalize_text(round1_votes.get(initiator_party), max_len=64))
    counterparty_choice = _find_option_by_id(round1_options, _normalize_text(round1_votes.get(counterparty_party), max_len=64))

    initiator_comp = initiator_choice.get("compensation_rub") if initiator_choice else None
    counterparty_comp = counterparty_choice.get("compensation_rub") if counterparty_choice else None

    numeric_values = [
        item.get("compensation_rub")
        for item in round1_options
        if isinstance(item.get("compensation_rub"), int)
    ]
    if not numeric_values:
        return options

    midpoint = int(round((min(numeric_values) + max(numeric_values)) / 2.0))
    adjusted: List[Dict[str, Any]] = []

    for option in options:
        updated = dict(option)
        comp = updated.get("compensation_rub")
        if not isinstance(comp, int):
            adjusted.append(updated)
            continue

        # Универсальное сжатие к midpoint, чтобы второй раунд был объективно более компромиссным.
        comp = int(round(midpoint + (comp - midpoint) * 0.65))

        lean = str(updated.get("lean") or "").lower()

        # Дополнительные гарантийные ограничения относительно выбранных крайних опций 1-го раунда.
        if lean == "initiator_favor" and isinstance(initiator_comp, int):
            if initiator_party == "customer":
                comp = min(comp, max(0, initiator_comp - max(1, int(round(max(initiator_comp, 1) * 0.1)))))
            else:
                comp = max(comp, min(initiator_comp + max(1, int(round(max(initiator_comp, 1) * 0.1))), midpoint))
        elif lean == "counterparty_favor" and isinstance(counterparty_comp, int):
            if counterparty_party == "customer":
                comp = min(comp, max(0, counterparty_comp - max(1, int(round(max(counterparty_comp, 1) * 0.1)))))
            else:
                comp = max(comp, min(counterparty_comp + max(1, int(round(max(counterparty_comp, 1) * 0.1))), midpoint))
        elif lean == "compromise":
            comp = midpoint

        updated["compensation_rub"] = max(0, comp)
        adjusted.append(updated)

    return adjusted


def _should_force_questions(
    *,
    dispute: Dict[str, Any],
    model_input: Dict[str, Any],
    round_number: int,
) -> bool:
    if round_number != 1:
        return False

    if dispute.get("clarification_answers"):
        return False

    uncertainty_hints = [str(item).strip() for item in (model_input.get("uncertainty_hints") or []) if str(item).strip()]
    if uncertainty_hints:
        return True

    chat_history = model_input.get("chat_history") or []
    if len(chat_history) < 5:
        return True

    announcement = model_input.get("announcement_context") or {}
    if len(_normalize_long_text(announcement.get("description"), max_len=2500)) < 40:
        return True

    initiator_form = dispute.get("initiator_form") or {}
    counterparty_form = dispute.get("counterparty_form") or {}
    if len(_normalize_long_text(initiator_form.get("problem_description"), max_len=1200)) < 70:
        return True
    if len(_normalize_long_text(counterparty_form.get("response_description"), max_len=1200)) < 70:
        return True

    return False


def _contextual_fallback_questions(dispute: Dict[str, Any], model_input: Dict[str, Any]) -> List[Dict[str, Any]]:
    announcement = model_input.get("announcement_context") or {}
    title = _normalize_text(announcement.get("title"), max_len=160) or "текущему заданию"
    initiator_form = dispute.get("initiator_form") or {}
    counterparty_form = dispute.get("counterparty_form") or {}
    desired_initiator = _normalize_text(initiator_form.get("desired_resolution"), max_len=64) or "урегулированию"
    desired_counterparty = _normalize_text(counterparty_form.get("desired_resolution"), max_len=64) or "урегулированию"

    return [
        {
            "id": "q1",
            "addressed_party": "both",
            "text": f"По заданию «{title}»: какие конкретные условия результата стороны считали обязательными до начала работы?",
        },
        {
            "id": "q2",
            "addressed_party": "initiator",
            "text": f"Какие 1–2 пункта по заданию «{title}» не выполнены, и как это повлияло на ваш запрос по формату {desired_initiator}?",
        },
        {
            "id": "q3",
            "addressed_party": "counterparty",
            "text": f"Что именно было фактически выполнено по «{title}», и почему вы считаете формат {desired_counterparty} справедливым?",
        },
    ]


def _option_effective_compensation(option: Dict[str, Any], requested_compensation_rub: int) -> int:
    compensation = option.get("compensation_rub")
    if isinstance(compensation, int):
        return max(0, compensation)

    refund_percent = option.get("refund_percent")
    if isinstance(refund_percent, int) and requested_compensation_rub > 0:
        return _compensation_from_percent(requested_compensation_rub, refund_percent)
    return 0


def _enrich_options_with_context(
    *,
    options: List[Dict[str, Any]],
    dispute: Dict[str, Any],
    model_input: Dict[str, Any],
    summary: str,
    round_number: int,
) -> List[Dict[str, Any]]:
    if not options:
        return options

    announcement = model_input.get("announcement_context") or {}
    chat_history = model_input.get("chat_history") or []
    chat_tail = [item for item in chat_history[-6:] if isinstance(item, dict)]
    chat_signal = " ".join(_normalize_long_text(item.get("text"), max_len=180) for item in chat_tail[:3] if item.get("text"))

    announcement_title = _normalize_text(announcement.get("title"), max_len=180) or "текущему заданию"
    announcement_description = _normalize_long_text(announcement.get("description"), max_len=500)

    initiator_form = dispute.get("initiator_form") or {}
    counterparty_form = dispute.get("counterparty_form") or {}
    requested_compensation = max(0, int(initiator_form.get("requested_compensation_rub") or 0))
    acceptable_percent = max(0, min(100, int(counterparty_form.get("acceptable_refund_percent") or 0)))

    enriched: List[Dict[str, Any]] = []
    for index, item in enumerate(options, start=1):
        option = dict(item)
        compensation = _option_effective_compensation(option, requested_compensation)
        compensation_text = f"{compensation} ₽" if compensation > 0 else "денежная часть минимальная"
        round_hint = "в первом раунде" if round_number == 1 else "во втором, более строгом раунде"

        base_context = (
            f"По заданию «{announcement_title}» этот вариант учитывает факты из чата и анкеты сторон {round_hint}. "
            f"Цель — закрыть спор без эскалации к модератору."
        )
        if announcement_description:
            base_context += f" Контекст задания: {announcement_description[:220]}."
        if chat_signal:
            base_context += f" Ключевой сигнал из чата: {chat_signal[:240]}."

        customer_reason = (
            f"Вы запрашивали до {requested_compensation} ₽, а здесь фиксируется {compensation_text}. "
            f"Это позволяет получить измеримый результат и быстро завершить спор по кейсу «{announcement_title}»."
        )
        performer_reason = (
            f"Вы указывали готовность к {acceptable_percent}% и аргументировали объём выполненной части. "
            f"Здесь обязательство ограничено уровнем {compensation_text}, что снижает риск дальнейшей эскалации."
        )

        desc = _normalize_long_text(option.get("description"), max_len=900)
        if len(desc) < 80 or "компромисс" in desc.lower():
            desc = f"{base_context} Почему это рабочий вариант: {summary}"
        else:
            desc = f"{desc} {base_context}"

        customer_action = _normalize_long_text(option.get("customer_action"), max_len=900)
        if len(customer_action) < 60:
            customer_action = customer_reason
        else:
            customer_action = f"{customer_action} {customer_reason}"

        performer_action = _normalize_long_text(option.get("performer_action"), max_len=900)
        if len(performer_action) < 60:
            performer_action = performer_reason
        else:
            performer_action = f"{performer_action} {performer_reason}"

        option["description"] = desc
        option["customer_action"] = customer_action
        option["performer_action"] = performer_action
        option["id"] = option.get("id") or f"r{round_number}_opt_{index}"
        enriched.append(option)

    return enriched


def _normalize_llm_response(
    *,
    response_obj: Optional[Dict[str, Any]],
    dispute: Dict[str, Any],
    round_number: int,
    model_input: Dict[str, Any],
) -> Dict[str, Any]:
    order_amount = max(0, int(dispute["initiator_form"].get("requested_compensation_rub") or 0))

    response_type = ""
    summary = ""
    questions: List[Dict[str, Any]] = []
    options: List[Dict[str, Any]] = []

    if isinstance(response_obj, dict):
        response_type = _normalize_text(response_obj.get("response_type"), max_len=64).lower()
        summary = _normalize_long_text(response_obj.get("summary"), max_len=700)
        questions = _normalize_questions(response_obj.get("questions"))
        options = _normalize_options(response_obj.get("settlement_options"), order_amount=max(order_amount, 1_000_000))

    if not summary:
        summary = "Модель подготовила обновлённый ответ по спору."

    expected_round_type = "settlement_options_round_1" if round_number == 1 else "settlement_options_round_2"

    force_questions = isinstance(response_obj, dict) and _should_force_questions(
        dispute=dispute,
        model_input=model_input,
        round_number=round_number,
    )
    if (response_type == "questions" or force_questions) and round_number == 1:
        if not questions:
            questions = _contextual_fallback_questions(dispute, model_input)
        return {
            "response_type": "questions",
            "summary": summary,
            "questions": questions[:5],
            "settlement_options": [],
        }

    if len(options) < 3:
        options = _fallback_options_round_1(dispute, summary=summary) if round_number == 1 else _fallback_options_round_2(dispute, summary=summary)

    if round_number == 2:
        options = _enforce_round2_compromise(dispute, options)

    # Строго 3 опции, с гарантированной разметкой lean.
    ordered: List[Dict[str, Any]] = []
    by_lean: Dict[str, List[Dict[str, Any]]] = {"initiator_favor": [], "counterparty_favor": [], "compromise": []}
    for item in options:
        lean = str(item.get("lean") or "compromise").lower()
        if lean not in by_lean:
            lean = "compromise"
        by_lean[lean].append(item)

    for lean in ("initiator_favor", "counterparty_favor", "compromise"):
        if by_lean[lean]:
            ordered.append(by_lean[lean][0])

    idx = 0
    while len(ordered) < 3 and idx < len(options):
        candidate = options[idx]
        if candidate not in ordered:
            ordered.append(candidate)
        idx += 1

    if len(ordered) < 3:
        fallback = _fallback_options_round_1(dispute, summary=summary) if round_number == 1 else _fallback_options_round_2(dispute, summary=summary)
        for candidate in fallback:
            if len(ordered) >= 3:
                break
            ordered.append(candidate)

    ordered = _enrich_options_with_context(
        options=ordered[:3],
        dispute=dispute,
        model_input=model_input,
        summary=summary,
        round_number=round_number,
    )

    normalized_options = []
    for index, item in enumerate(ordered, start=1):
        normalized_options.append(
            _normalize_option(
                {
                    **item,
                    "id": item.get("id") or f"r{round_number}_opt_{index}",
                },
                fallback_id=f"r{round_number}_opt_{index}",
                order_amount=max(order_amount, 1_000_000),
            )
        )

    return {
        "response_type": expected_round_type,
        "summary": summary,
        "questions": [],
        "settlement_options": normalized_options,
    }


def _groq_chat_completions_endpoint() -> str:
    base = DISPUTE_GROQ_BASE_URL.rstrip("/")
    return f"{base}/chat/completions"


def _build_announcement_context(thread_id: str) -> Dict[str, Any]:
    row = fetch_one(
        """
        SELECT
            COALESCE(ct.task_id::text, ta.task_id::text, tf.task_id::text) AS announcement_id,
            COALESCE(t.title, a.title, '') AS title,
            COALESCE(t.description, a.data->>'description', '') AS description,
            COALESCE(t.status, a.status, '') AS status,
            COALESCE(t.price_type, a.data->>'price_type', '') AS price_type,
            COALESCE(t.currency, a.data->>'currency', 'RUB') AS currency,
            t.reward_amount::text,
            t.budget_min::text,
            t.budget_max::text,
            COALESCE(t.customer_comment, a.data->>'customer_comment', '') AS customer_comment,
            COALESCE(ta.execution_stage, '') AS execution_stage,
            COALESCE(ta.assignment_status, '') AS assignment_status,
            COALESCE(tf.message, '') AS offer_message
        FROM chat_threads ct
        LEFT JOIN task_assignments ta
          ON ta.id = ct.assignment_id
        LEFT JOIN task_offers tf
          ON tf.id = COALESCE(ct.offer_id, ta.offer_id)
        LEFT JOIN tasks t
          ON t.id = COALESCE(ct.task_id, ta.task_id, tf.task_id)
        LEFT JOIN announcements a
          ON a.id::text = COALESCE(ct.task_id::text, ta.task_id::text, tf.task_id::text)
        WHERE ct.id::text = %s
        LIMIT 1
        """,
        (thread_id,),
    )
    if not row:
        return {}

    return {
        "announcement_id": _normalize_text(row[0], max_len=64),
        "title": _normalize_long_text(row[1], max_len=240),
        "description": _normalize_long_text(row[2], max_len=2500),
        "status": _normalize_text(row[3], max_len=64),
        "price_type": _normalize_text(row[4], max_len=64),
        "currency": _normalize_text(row[5], max_len=16),
        "reward_amount": _normalize_text(row[6], max_len=64),
        "budget_min": _normalize_text(row[7], max_len=64),
        "budget_max": _normalize_text(row[8], max_len=64),
        "customer_comment": _normalize_long_text(row[9], max_len=1200),
        "execution_stage": _normalize_text(row[10], max_len=64),
        "assignment_status": _normalize_text(row[11], max_len=64),
        "offer_message": _normalize_long_text(row[12], max_len=1200),
    }


def _chat_uncertainty_hints(
    *,
    chat_history: List[Dict[str, Any]],
    initiator_form: Dict[str, Any],
    counterparty_form: Dict[str, Any],
    announcement_context: Dict[str, Any],
) -> List[str]:
    hints: List[str] = []

    if len(chat_history) < 6:
        hints.append("Короткая история чата: недостаточно фактов о ходе выполнения.")

    initiator_description = _normalize_long_text(initiator_form.get("problem_description"), max_len=1200)
    counterparty_description = _normalize_long_text(counterparty_form.get("response_description"), max_len=1200)
    if len(initiator_description) < 60:
        hints.append("Инициатор описал проблему слишком кратко.")
    if len(counterparty_description) < 60:
        hints.append("Встречная сторона описала позицию слишком кратко.")

    if not _normalize_text(announcement_context.get("title"), max_len=200):
        hints.append("Нет надёжного заголовка задания.")
    if len(_normalize_long_text(announcement_context.get("description"), max_len=2500)) < 40:
        hints.append("Недостаточно деталей по самому объявлению/заданию.")

    initiator_amount = int(initiator_form.get("requested_compensation_rub") or 0)
    acceptable_percent = int(counterparty_form.get("acceptable_refund_percent") or 0)
    if initiator_amount > 0 and acceptable_percent == 0:
        hints.append("Сильный разрыв по ожиданиям компенсации без объяснённых критериев.")

    return hints[:5]


def _build_model_input(dispute: Dict[str, Any], round_number: int) -> Dict[str, Any]:
    thread_id = dispute["thread_id"]

    chat_rows = fetch_all(
        """
        SELECT
            COALESCE(sender_display_name, sender_label, 'Участник') AS sender_name,
            COALESCE(sender_type, 'user') AS sender_type,
            text,
            created_at
        FROM chat_messages
        WHERE thread_id::text = %s
          AND deleted_at IS NULL
          AND type = 'text'
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (thread_id,),
    )

    chat_history = [
        {
            "sender_name": str(row[0] or "Участник"),
            "sender_type": str(row[1] or "user"),
            "text": _normalize_long_text(row[2], max_len=1500),
            "created_at": row[3].isoformat() if isinstance(row[3], datetime) else None,
        }
        for row in reversed(chat_rows)
    ]

    initiator_form = dispute.get("initiator_form") or {}
    counterparty_form = dispute.get("counterparty_form") or {}
    announcement_context = _build_announcement_context(thread_id)
    uncertainty_hints = _chat_uncertainty_hints(
        chat_history=chat_history,
        initiator_form=initiator_form,
        counterparty_form=counterparty_form,
        announcement_context=announcement_context,
    )

    return {
        "dispute_id": dispute["id"],
        "thread_id": thread_id,
        "round_number": round_number,
        "status": dispute["status"],
        "initiator_party_role": dispute["initiator_party_role"],
        "initiator_form": initiator_form,
        "counterparty_form": counterparty_form,
        "clarifying_questions": dispute.get("clarifying_questions") or [],
        "clarification_answers": dispute.get("clarification_answers") or {},
        "round1_options": dispute.get("round1_options") or [],
        "round1_votes": dispute.get("round1_votes") or {},
        "announcement_context": announcement_context,
        "chat_history": chat_history,
        "uncertainty_hints": uncertainty_hints,
        "constraints": {
            "chat_history_limit": 50,
            "ignore_media": True,
            "required_language": "ru",
            "second_round_must_be_more_compromise": True,
            "ask_questions_when_uncertain": True,
        },
    }


def _call_groq(model_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not DISPUTE_GROQ_API_KEY:
        logger.warning("dispute_groq_key_missing", extra={"status_code": 0})
        return None

    system_prompt = get_env("DISPUTE_GROQ_PROMPT", SYSTEM_PROMPT_FALLBACK) or SYSTEM_PROMPT_FALLBACK
    model_name = get_env("DISPUTE_GROQ_MODEL", DEFAULT_DISPUTE_GROQ_MODEL) or DEFAULT_DISPUTE_GROQ_MODEL
    endpoint = _groq_chat_completions_endpoint()

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "top_p": 0.8,
        "response_format": {"type": "json_object"},
    }

    def _invoke() -> Optional[Dict[str, Any]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {DISPUTE_GROQ_API_KEY}",
            },
            method="POST",
        )

        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=DISPUTE_RESPONSE_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            raise RuntimeError(f"Groq HTTP {exc.code}: {detail[:500]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Groq transport error: {exc}") from exc

        parsed = json.loads(raw)
        text_parts: List[str] = []
        for choice in parsed.get("choices", []) or []:
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])

        model_text = _strip_markdown_fences("\n".join(text_parts))
        if not model_text:
            raise RuntimeError("Groq returned empty body")

        try:
            obj = json.loads(model_text)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Groq returned invalid JSON: {model_text[:400]}") from exc

        elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        logger.info(
            "dispute_groq_success",
            extra={"elapsed_ms": elapsed_ms, "model": model_name, "status_code": 0},
        )
        if isinstance(obj, dict):
            return obj
        raise RuntimeError("Groq JSON root must be object")

    result = call_external_sync(
        "groq_dispute",
        _invoke,
        retries=DISPUTE_GROQ_RETRIES,
        fallback=None,
    )
    if isinstance(result, dict):
        return result
    return None


def _acquire_model_slot(dispute_id: str) -> bool:
    with _MODEL_INFLIGHT_LOCK:
        if dispute_id in _MODEL_INFLIGHT:
            return False
        _MODEL_INFLIGHT.add(dispute_id)
        return True


def _release_model_slot(dispute_id: str) -> None:
    with _MODEL_INFLIGHT_LOCK:
        _MODEL_INFLIGHT.discard(dispute_id)


def process_dispute_model_turn(dispute_id: str) -> None:
    if not _acquire_model_slot(dispute_id):
        return

    try:
        dispute = fetch_one(
            _dispute_select_sql()
            + """
            WHERE d.id::text = %s
            LIMIT 1
            """,
            (dispute_id,),
        )
        if not dispute:
            return

        dispute_dict = _row_to_dispute_dict(dispute)
        if dispute_dict["status"] != DISPUTE_STATUS_MODEL_THINKING:
            return

        round_number = int(dispute_dict.get("active_round") or 1)
        model_input = _build_model_input(dispute_dict, round_number)

        execute(
            """
            UPDATE disputes
            SET model_attempts = model_attempts + 1,
                last_model_error = NULL,
                updated_at = now()
            WHERE id::text = %s
            """,
            (dispute_id,),
        )

        llm_obj = _call_groq(model_input)
        normalized = _normalize_llm_response(
            response_obj=llm_obj,
            dispute=dispute_dict,
            round_number=round_number,
            model_input=model_input,
        )

        summary = _normalize_long_text(normalized.get("summary") or "", max_len=700)
        questions = normalized.get("questions") or []
        settlement_options = normalized.get("settlement_options") or []

        if normalized["response_type"] == "questions" and round_number == 1:
            execute(
                """
                UPDATE disputes
                SET status = %s,
                    clarifying_questions = %s::jsonb,
                    clarification_answers = '{}'::jsonb,
                    updated_at = now()
                WHERE id::text = %s
                """,
                (
                    DISPUTE_STATUS_WAITING_CLARIFICATIONS,
                    json.dumps(questions, ensure_ascii=False),
                    dispute_id,
                ),
            )
            _insert_dispute_event(dispute_id, "questions_requested", None, {"count": len(questions), "summary": summary})

            numbered = "\n".join(f"{idx}. {q['text']}" for idx, q in enumerate(questions, start=1))
            _safe_post_system_message(
                dispute_dict["thread_id"],
                f"Модель запросила уточнения. {summary}\n\n{numbered}",
            )
            return

        if round_number == 1:
            execute(
                """
                UPDATE disputes
                SET status = %s,
                    round1_options = %s::jsonb,
                    round1_votes = '{}'::jsonb,
                    clarifying_questions = '[]'::jsonb,
                    clarification_answers = '{}'::jsonb,
                    updated_at = now()
                WHERE id::text = %s
                """,
                (
                    DISPUTE_STATUS_WAITING_ROUND_1_VOTES,
                    json.dumps(settlement_options, ensure_ascii=False),
                    dispute_id,
                ),
            )
            _insert_dispute_event(dispute_id, "round1_options_ready", None, {"summary": summary})
            _safe_post_system_message(
                dispute_dict["thread_id"],
                f"Модель подготовила 3 варианта урегулирования (раунд 1). {summary}",
            )
            return

        execute(
            """
            UPDATE disputes
            SET status = %s,
                round2_options = %s::jsonb,
                round2_votes = '{}'::jsonb,
                updated_at = now()
            WHERE id::text = %s
            """,
            (
                DISPUTE_STATUS_WAITING_ROUND_2_VOTES,
                json.dumps(settlement_options, ensure_ascii=False),
                dispute_id,
            ),
        )
        _insert_dispute_event(dispute_id, "round2_options_ready", None, {"summary": summary})
        _safe_post_system_message(
            dispute_dict["thread_id"],
            f"Модель подготовила 3 более компромиссных варианта (раунд 2). {summary}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispute_model_cycle_failed",
            extra={"dispute_id": dispute_id, "error": str(exc), "status_code": 0},
        )
        execute(
            """
            UPDATE disputes
            SET status = %s,
                last_model_error = %s,
                updated_at = now()
            WHERE id::text = %s
            """,
            (
                DISPUTE_STATUS_AWAITING_MODERATOR,
                _normalize_long_text(str(exc), max_len=2000),
                dispute_id,
            ),
        )
        row = fetch_one(
            _dispute_select_sql() + " WHERE d.id::text = %s LIMIT 1",
            (dispute_id,),
        )
        if row:
            dispute_dict = _row_to_dispute_dict(row)
            _safe_post_system_message(
                dispute_dict["thread_id"],
                "Автоматический разбор спора временно недоступен. Спор отмечен для подключения модератора.",
            )
    finally:
        _release_model_slot(dispute_id)


def select_settlement_option(
    *,
    thread_id: str,
    dispute_id: str,
    actor_user_id: str,
    option_id: str,
) -> Tuple[Dict[str, Any], bool]:
    dispute = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    if dispute["status"] not in {DISPUTE_STATUS_WAITING_ROUND_1_VOTES, DISPUTE_STATUS_WAITING_ROUND_2_VOTES}:
        raise HTTPException(status_code=409, detail="Dispute is not waiting for option votes")

    actor_party = _party_role_for_user(dispute, actor_user_id)
    if actor_party not in {"customer", "performer"}:
        raise HTTPException(status_code=403, detail="Only dispute participants can vote")

    normalized_option_id = _normalize_text(option_id, max_len=64)
    if not normalized_option_id:
        raise HTTPException(status_code=400, detail="option_id is required")

    options = _active_options(dispute)
    if not any(_normalize_text(item.get("id"), max_len=64) == normalized_option_id for item in options):
        raise HTTPException(status_code=400, detail="Unknown option_id for current round")

    votes = _active_votes(dispute)
    votes[actor_party] = normalized_option_id

    if dispute["active_round"] == 2:
        execute(
            """
            UPDATE disputes
            SET round2_votes = %s::jsonb,
                updated_at = now()
            WHERE id::text = %s
            """,
            (json.dumps(votes, ensure_ascii=False), dispute_id),
        )
    else:
        execute(
            """
            UPDATE disputes
            SET round1_votes = %s::jsonb,
                updated_at = now()
            WHERE id::text = %s
            """,
            (json.dumps(votes, ensure_ascii=False), dispute_id),
        )

    _insert_dispute_event(
        dispute_id,
        "vote_submitted",
        actor_user_id,
        {"party_role": actor_party, "option_id": normalized_option_id, "round": dispute["active_round"]},
    )

    should_start_model = False

    if "customer" in votes and "performer" in votes:
        customer_choice = votes["customer"]
        performer_choice = votes["performer"]

        if customer_choice == performer_choice:
            execute(
                """
                UPDATE disputes
                SET status = %s,
                    selected_option_id = %s,
                    resolution_summary = %s,
                    closed_at = now(),
                    updated_at = now()
                WHERE id::text = %s
                """,
                (
                    DISPUTE_STATUS_RESOLVED,
                    customer_choice,
                    "Обе стороны выбрали один и тот же вариант. Спор закрыт.",
                    dispute_id,
                ),
            )
            _insert_dispute_event(dispute_id, "resolved", actor_user_id, {"option_id": customer_choice})
            _safe_post_system_message(thread_id, "Стороны выбрали одинаковый вариант. Спор закрыт.")
        else:
            if dispute["active_round"] == 1:
                execute(
                    """
                    UPDATE disputes
                    SET status = %s,
                        active_round = 2,
                        updated_at = now()
                    WHERE id::text = %s
                    """,
                    (DISPUTE_STATUS_MODEL_THINKING, dispute_id),
                )
                _insert_dispute_event(
                    dispute_id,
                    "round2_requested",
                    actor_user_id,
                    {"customer_choice": customer_choice, "performer_choice": performer_choice},
                )
                _safe_post_system_message(
                    thread_id,
                    "Стороны выбрали разные варианты в раунде 1. Модель формирует второй, более компромиссный раунд…",
                )
                should_start_model = True
            else:
                execute(
                    """
                    UPDATE disputes
                    SET status = %s,
                        moderator_hook = %s::jsonb,
                        resolution_summary = %s,
                        closed_at = now(),
                        updated_at = now()
                    WHERE id::text = %s
                    """,
                    (
                        DISPUTE_STATUS_AWAITING_MODERATOR,
                        json.dumps({"status": "pending", "reason": "round2_mismatch"}, ensure_ascii=False),
                        "После второго раунда стороны не пришли к согласию. Требуется модератор.",
                        dispute_id,
                    ),
                )
                _insert_dispute_event(
                    dispute_id,
                    "awaiting_moderator",
                    actor_user_id,
                    {"customer_choice": customer_choice, "performer_choice": performer_choice},
                )
                _safe_post_system_message(
                    thread_id,
                    "После второго раунда согласия нет. Автоматическая часть спора завершена, спор ожидает модератора.",
                )
    else:
        waiting_for = "заказчика" if "customer" not in votes else "исполнителя"
        _safe_post_system_message(thread_id, f"Голос получен. Ожидаем выбор от {waiting_for}.")

    updated = _fetch_dispute_row_by_id(thread_id, dispute_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Dispute update failed")

    return _build_dispute_state_out(updated, actor_user_id), should_start_model


def capture_clarification_answer_from_chat_message(
    *,
    thread_id: str,
    sender_user_id: str,
    message_id: str,
    message_text: str,
) -> Optional[str]:
    dispute = _fetch_active_dispute_row(thread_id)
    if not dispute:
        return None

    if dispute["status"] != DISPUTE_STATUS_WAITING_CLARIFICATIONS:
        return None

    sender_party = _party_role_for_user(dispute, sender_user_id)
    if sender_party not in {"customer", "performer"}:
        return None

    required_roles = _required_answer_roles(dispute)
    if sender_party not in required_roles:
        return None

    answers = dispute.get("clarification_answers") or {}
    if sender_party in answers:
        return None

    normalized_text = _normalize_long_text(message_text, max_len=5000)
    if not normalized_text:
        return None

    answers[sender_party] = {
        "message_id": message_id,
        "text": normalized_text,
        "created_at": _now_utc().isoformat(),
    }

    execute(
        """
        UPDATE disputes
        SET clarification_answers = %s::jsonb,
            updated_at = now()
        WHERE id::text = %s
        """,
        (json.dumps(answers, ensure_ascii=False), dispute["id"]),
    )

    _insert_dispute_event(
        dispute["id"],
        "clarification_answer_recorded",
        sender_user_id,
        {"party_role": sender_party, "message_id": message_id},
    )

    if all(role in answers for role in required_roles):
        execute(
            """
            UPDATE disputes
            SET status = %s,
                updated_at = now()
            WHERE id::text = %s
            """,
            (DISPUTE_STATUS_MODEL_THINKING, dispute["id"]),
        )
        _insert_dispute_event(dispute["id"], "clarification_answers_completed", None, {"required_roles": sorted(required_roles)})
        _safe_post_system_message(thread_id, "Все обязательные ответы на вопросы модели получены. Модель продолжает анализ…")
        return dispute["id"]

    missing = [role for role in sorted(required_roles) if role not in answers]
    missing_human = " и ".join("заказчика" if role == "customer" else "исполнителя" for role in missing)
    _safe_post_system_message(thread_id, f"Ответ зафиксирован. Ожидаем первый ответ от {missing_human}.")
    return None
