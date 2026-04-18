from __future__ import annotations

import copy
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, Optional


TASK_PUBLIC_STATUSES = {"published", "in_responses"}
TASK_ASSIGNMENT_ACTIVE_STATUSES = {"assigned", "in_progress"}
EXECUTION_CUSTOMER_VISIBLE = {"en_route", "on_site", "in_progress", "handoff"}
EXECUTION_TERMINAL = {"completed", "cancelled"}

LEGACY_TO_CANONICAL_OFFER_STATUS = {
    "pending": "sent",
    "accepted": "accepted_by_customer",
    "rejected": "rejected_by_customer",
    "withdrawn": "withdrawn_by_sender",
}

CANONICAL_TO_LEGACY_OFFER_STATUS = {
    "sent": "pending",
    "accepted_by_customer": "accepted",
    "rejected_by_customer": "rejected",
    "withdrawn_by_sender": "withdrawn",
    "cancelled_by_sender": "withdrawn",
}


def is_uuid_like(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


def normalize_optional_text(value: Any, *, collapse_spaces: bool = False) -> Optional[str]:
    if value is None:
        return None

    normalized = str(value).strip()
    if collapse_spaces:
        normalized = " ".join(normalized.split())
    return normalized or None


def normalize_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {}


def normalize_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return []


def parse_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(" ", "").replace(",", ".")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def parse_int(value: Any) -> Optional[int]:
    parsed = parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def extract_point(value: Any) -> Optional[tuple[float, float]]:
    if not isinstance(value, dict):
        return None

    lat = parse_float(value.get("lat"))
    lon = parse_float(value.get("lon"))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def point_json(point: tuple[float, float]) -> Dict[str, float]:
    return {"lat": point[0], "lon": point[1]}


def current_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def builder_category_slug(raw_category: Optional[str]) -> str:
    normalized = (raw_category or "").strip().lower()
    if normalized == "delivery":
        return "delivery"
    if normalized in {"help", "errands"}:
        return "errands"
    if normalized in {"shopping", "buy"}:
        return "shopping"
    return "errands"


def announcement_status_to_task_fields(
    announcement_status: Optional[str],
    *,
    deleted: bool = False,
    has_accepted_offer: bool = False,
) -> tuple[str, str]:
    if deleted:
        return "closed", "published"

    normalized = (announcement_status or "").strip().lower()
    if normalized in {"pending_review", "pending", "review", "in_review"}:
        return "review", "pending"
    if normalized == "needs_fix":
        return "draft", "needs_fix"
    if normalized in {"rejected", "declined"}:
        return "draft", "rejected"
    if normalized == "archived":
        return "closed", "published"
    if normalized in {"completed", "done"}:
        return "completed", "published"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled", "published"
    if normalized in {"assigned", "agreed"}:
        return "agreed", "published"
    if normalized in {"in_progress", "executing"}:
        return "in_progress", "published"
    if normalized in {"active", "published", "open"}:
        return ("agreed" if has_accepted_offer else "published"), "published"
    return "draft", "pending"


def task_to_announcement_status(
    task_status: Optional[str],
    moderation_status: Optional[str],
    deleted_at: Any,
    *,
    assignment_status: Optional[str] = None,
    execution_stage: Optional[str] = None,
) -> str:
    if deleted_at is not None:
        return "deleted"

    moderation = (moderation_status or "").strip().lower()
    if moderation == "pending":
        return "pending_review"
    if moderation == "needs_fix":
        return "needs_fix"
    if moderation in {"rejected", "blocked"}:
        return "rejected"

    normalized_task_status = (task_status or "").strip().lower()
    normalized_assignment_status = (assignment_status or "").strip().lower()
    normalized_execution_stage = canonical_execution_status(
        execution_stage=execution_stage,
        assignment_status=normalized_assignment_status,
        current_value=None,
    )

    if normalized_task_status == "closed":
        return "archived"
    if normalized_task_status == "cancelled" or normalized_execution_stage == "cancelled":
        return "cancelled"
    if normalized_task_status == "completed" or normalized_execution_stage == "completed":
        return "completed"
    if normalized_task_status == "in_progress" or normalized_execution_stage in EXECUTION_CUSTOMER_VISIBLE - {"completed"}:
        return "in_progress"
    if normalized_task_status == "agreed" or normalized_assignment_status == "assigned":
        return "assigned"
    if normalized_task_status in TASK_PUBLIC_STATUSES:
        return "active"
    if normalized_task_status in {"draft", "review", "deferred"}:
        return "pending_review"
    return "active"


def canonical_execution_status(
    *,
    execution_stage: Optional[str],
    assignment_status: Optional[str],
    current_value: Optional[str],
) -> str:
    explicit = normalize_optional_text(execution_stage)
    if explicit:
        normalized = explicit.lower()
        if normalized in {
            "open",
            "accepted",
            "awaiting_acceptance",
            "en_route",
            "on_site",
            "in_progress",
            "handoff",
            "completed",
            "cancelled",
            "disputed",
        }:
            return normalized

    assignment = (assignment_status or "").strip().lower()
    if assignment == "in_progress":
        return "in_progress"
    if assignment == "assigned":
        return "accepted"
    if assignment == "completed":
        return "completed"
    if assignment == "cancelled":
        return "cancelled"

    current = (current_value or "").strip().lower()
    return current or "open"


def route_visibility_for_execution(execution_stage: Optional[str]) -> str:
    normalized = canonical_execution_status(
        execution_stage=execution_stage,
        assignment_status=None,
        current_value=None,
    )
    if normalized in EXECUTION_TERMINAL:
        return "hidden"
    if normalized in EXECUTION_CUSTOMER_VISIBLE:
        return "customer_visible"
    return "performer_only"


def legacy_offer_status_to_canonical(status: Optional[str]) -> str:
    normalized = (status or "").strip().lower()
    return LEGACY_TO_CANONICAL_OFFER_STATUS.get(normalized, "sent")


def canonical_offer_status_to_legacy(status: Optional[str]) -> str:
    normalized = (status or "").strip().lower()
    return CANONICAL_TO_LEGACY_OFFER_STATUS.get(normalized, "pending")


def primary_source_address(data: Dict[str, Any]) -> Optional[str]:
    for key in ("pickup_address", "address", "source_address", "start_address", "address_text"):
        value = normalize_optional_text(data.get(key), collapse_spaces=True)
        if value:
            return value
    return None


def primary_destination_address(data: Dict[str, Any]) -> Optional[str]:
    for key in ("dropoff_address", "destination_address", "end_address", "to_address"):
        value = normalize_optional_text(data.get(key), collapse_spaces=True)
        if value:
            return value
    return None


def primary_map_point(data: Dict[str, Any]) -> Optional[tuple[float, float]]:
    for key in ("point", "pickup_point", "help_point", "source_point"):
        point = extract_point(data.get(key))
        if point:
            return point
    return None


def destination_point(data: Dict[str, Any]) -> Optional[tuple[float, float]]:
    for key in ("dropoff_point", "destination_point", "end_point", "to_point"):
        point = extract_point(data.get(key))
        if point:
            return point
    return None


def derive_quick_offer_price(data: Dict[str, Any]) -> Optional[int]:
    task = normalize_json_object(data.get("task"))
    offer_policy = normalize_json_object(task.get("offer_policy"))
    budget = normalize_json_object(task.get("budget"))
    return (
        parse_int(offer_policy.get("quick_offer_price"))
        or parse_int(data.get("quick_offer_price"))
        or parse_int(data.get("budget_min"))
        or parse_int(budget.get("min"))
        or parse_int(data.get("budget"))
        or parse_int(budget.get("amount"))
        or parse_int(data.get("budget_max"))
        or parse_int(budget.get("max"))
    )


def derive_budget_bounds(data: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    task = normalize_json_object(data.get("task"))
    budget = normalize_json_object(task.get("budget"))
    budget_min = parse_int(budget.get("min")) or parse_int(data.get("budget_min"))
    budget_max = (
        parse_int(budget.get("max"))
        or parse_int(data.get("budget_max"))
        or parse_int(budget.get("amount"))
        or parse_int(data.get("budget"))
    )
    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        budget_min, budget_max = budget_max, budget_min
    return budget_min, budget_max


def derive_reward_amount(data: Dict[str, Any]) -> int:
    task = normalize_json_object(data.get("task"))
    budget = normalize_json_object(task.get("budget"))
    return max(
        0,
        parse_int(budget.get("amount"))
        or parse_int(data.get("budget"))
        or derive_quick_offer_price(data)
        or derive_budget_bounds(data)[0]
        or derive_budget_bounds(data)[1]
        or 0,
    )


def ensure_task_payload(
    raw_data: Dict[str, Any],
    *,
    title: str,
    announcement_status: str,
    deleted_at: Any = None,
    assignment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = normalize_json_object(raw_data)
    task = normalize_json_object(data.get("task"))
    builder = normalize_json_object(task.get("builder"))
    attributes = normalize_json_object(task.get("attributes"))
    budget = normalize_json_object(task.get("budget"))
    route = normalize_json_object(task.get("route"))
    contacts = normalize_json_object(task.get("contacts"))
    search = normalize_json_object(task.get("search"))
    offer_policy = normalize_json_object(task.get("offer_policy"))
    execution = normalize_json_object(task.get("execution"))
    lifecycle = normalize_json_object(task.get("lifecycle"))

    budget_min, budget_max = derive_budget_bounds(data)
    quick_offer_price = derive_quick_offer_price(data)
    source_address = primary_source_address(data)
    destination_address = primary_destination_address(data)
    source_point = primary_map_point(data)
    destination = destination_point(data)

    normalized_status = task_to_announcement_status(
        lifecycle.get("status"),
        normalize_optional_text(data.get("moderation"), collapse_spaces=False),
        deleted_at,
    )
    lifecycle_status = {
        "active": "open",
        "assigned": "assigned",
        "in_progress": "in_progress",
        "completed": "completed",
        "cancelled": "cancelled",
        "archived": "archived",
        "rejected": "rejected",
        "needs_fix": "needs_fix",
        "pending_review": "pending_review",
    }.get((announcement_status or normalized_status or "").strip().lower(), "open")

    assignment_info = normalize_json_object(assignment)
    execution_status = canonical_execution_status(
        execution_stage=assignment_info.get("execution_stage"),
        assignment_status=assignment_info.get("assignment_status"),
        current_value=normalize_optional_text(execution.get("status"))
        or normalize_optional_text(data.get("execution_status")),
    )

    builder.setdefault("main_group", normalize_optional_text(data.get("main_group")) or normalize_optional_text(data.get("category")) or "help")
    builder.setdefault("action_type", normalize_optional_text(data.get("action_type")) or normalize_optional_text(data.get("user_action_type")))
    builder.setdefault("resolved_category", normalize_optional_text(data.get("resolved_category")) or normalize_optional_text(data.get("category")))
    builder.setdefault("item_type", normalize_optional_text(data.get("item_type")))
    builder.setdefault("purchase_type", normalize_optional_text(data.get("purchase_type")))
    builder.setdefault("help_type", normalize_optional_text(data.get("help_type")))
    builder.setdefault("source_kind", normalize_optional_text(data.get("source_kind")))
    builder.setdefault("destination_kind", normalize_optional_text(data.get("destination_kind")))
    builder.setdefault("urgency", normalize_optional_text(data.get("urgency")))
    builder.setdefault("task_brief", normalize_optional_text(data.get("task_brief")))
    builder.setdefault("notes", normalize_optional_text(data.get("notes")))

    for key in (
        "requires_vehicle",
        "needs_trunk",
        "requires_careful_handling",
        "needs_loader",
        "requires_lift_to_floor",
        "has_elevator",
        "wait_on_site",
        "contactless",
        "requires_receipt",
        "requires_confirmation_code",
        "call_before_arrival",
        "photo_report_required",
    ):
        if key not in attributes and key in data:
            attributes[key] = data.get(key)

    attributes.setdefault("weight_category", normalize_optional_text(data.get("weight_category")))
    attributes.setdefault("size_category", normalize_optional_text(data.get("size_category")))
    attributes.setdefault(
        "cargo",
        {
            "length_cm": parse_int(data.get("cargo_length_cm")) or parse_int(data.get("cargo_length")),
            "width_cm": parse_int(data.get("cargo_width_cm")) or parse_int(data.get("cargo_width")),
            "height_cm": parse_int(data.get("cargo_height_cm")) or parse_int(data.get("cargo_height")),
        },
    )
    attributes.setdefault("estimated_task_minutes", parse_int(data.get("estimated_task_minutes")))
    attributes.setdefault("waiting_minutes", parse_int(data.get("waiting_minutes")))
    attributes.setdefault("floor", parse_int(data.get("floor")))

    budget.setdefault("currency", "RUB")
    budget.setdefault("recommended_min", parse_int(data.get("recommended_price_min")))
    budget.setdefault("recommended_max", parse_int(data.get("recommended_price_max")))
    budget["min"] = budget.get("min") if budget.get("min") is not None else budget_min
    budget["max"] = budget.get("max") if budget.get("max") is not None else budget_max
    budget["amount"] = budget.get("amount") if budget.get("amount") is not None else derive_reward_amount(data)

    route.setdefault("travel_mode", normalize_optional_text(data.get("travel_mode")) or "driving")
    route.setdefault("start_at", normalize_optional_text(data.get("start_at")))
    route.setdefault("has_end_time", bool(data.get("has_end_time")))
    route.setdefault("end_at", normalize_optional_text(data.get("end_at")))
    route.setdefault("timezone", normalize_optional_text(data.get("timezone")) or normalize_optional_text(data.get("schedule_timezone")))
    route.setdefault(
        "source",
        {
            "address": source_address,
            "kind": normalize_optional_text(data.get("source_kind")),
            "point": point_json(source_point) if source_point else None,
        },
    )
    route.setdefault(
        "destination",
        {
            "address": destination_address,
            "kind": normalize_optional_text(data.get("destination_kind")),
            "point": point_json(destination) if destination else None,
        },
    )

    contacts.setdefault("name", normalize_optional_text(data.get("contact_name")))
    contacts.setdefault("phone", normalize_optional_text(data.get("contact_phone")))
    contacts.setdefault("method", normalize_optional_text(data.get("contact_method")))
    contacts.setdefault("audience", normalize_optional_text(data.get("audience")))

    search.setdefault("generated_title", normalize_optional_text(data.get("generated_title")) or normalize_optional_text(title))
    search.setdefault(
        "generated_description",
        normalize_optional_text(data.get("generated_description"))
        or normalize_optional_text(data.get("description"))
        or normalize_optional_text(data.get("notes")),
    )
    search.setdefault("generated_tags", normalize_json_list(data.get("generated_tags")))
    search.setdefault("hints", normalize_json_list(data.get("ai_hints")))

    offer_policy.setdefault("quick_offer_enabled", True)
    offer_policy.setdefault("quick_offer_price", quick_offer_price)
    offer_policy.setdefault("counter_price_allowed", True)
    offer_policy.setdefault("reoffer_policy", normalize_optional_text(data.get("reoffer_policy")) or "blocked_after_reject")

    execution["status"] = execution_status
    execution["assignment_id"] = assignment_info.get("id")
    execution["performer_user_id"] = assignment_info.get("performer_id")
    if assignment_info.get("chat_thread_id"):
        execution["chat_thread_id"] = assignment_info.get("chat_thread_id")
    if assignment_info.get("route_visibility"):
        execution["route_visibility"] = assignment_info.get("route_visibility")

    lifecycle["status"] = lifecycle_status
    lifecycle["deleted_at"] = deleted_at.isoformat() if hasattr(deleted_at, "isoformat") else deleted_at

    task["schema_version"] = 2
    task["lifecycle"] = lifecycle
    task["builder"] = builder
    task["attributes"] = attributes
    task["budget"] = budget
    task["route"] = route
    task["contacts"] = contacts
    task["search"] = search
    task["offer_policy"] = offer_policy
    task["execution"] = execution
    task["assignment"] = {
        "assignment_status": assignment_info.get("assignment_status"),
        "execution_status": execution_status,
        "performer_user_id": assignment_info.get("performer_id"),
        "chat_thread_id": assignment_info.get("chat_thread_id"),
        "route_visibility": assignment_info.get("route_visibility"),
    }

    data["task"] = task
    data["offer_policy"] = offer_policy
    data["execution"] = execution
    data["search"] = search
    if search.get("generated_description"):
        data.setdefault("generated_description", search["generated_description"])
        data.setdefault("description", search["generated_description"])
    if budget_min is not None:
        data["budget_min"] = budget_min
    if budget_max is not None:
        data["budget_max"] = budget_max
    if quick_offer_price is not None:
        data["quick_offer_price"] = quick_offer_price
    if route.get("start_at"):
        data.setdefault("start_at", route["start_at"])
    if route.get("end_at"):
        data.setdefault("end_at", route["end_at"])
    if route.get("timezone"):
        data.setdefault("timezone", route["timezone"])
    if route.get("source", {}).get("address"):
        data.setdefault("source_address", route["source"]["address"])
    if route.get("destination", {}).get("address"):
        data.setdefault("destination_address", route["destination"]["address"])
    return data


def task_row_to_announcement_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    assignment = {
        "id": row.get("assignment_id"),
        "assignment_status": row.get("assignment_status"),
        "execution_stage": row.get("execution_stage"),
        "performer_id": row.get("assignment_performer_id"),
        "chat_thread_id": row.get("assignment_chat_thread_id"),
        "route_visibility": row.get("route_visibility"),
    }

    announcement_status = task_to_announcement_status(
        row.get("task_status"),
        row.get("moderation_status"),
        row.get("deleted_at"),
        assignment_status=row.get("assignment_status"),
        execution_stage=row.get("execution_stage"),
    )
    data = ensure_task_payload(
        normalize_json_object(row.get("extra")),
        title=row.get("title") or "",
        announcement_status=announcement_status,
        deleted_at=row.get("deleted_at"),
        assignment=assignment,
    )

    data["offers_count"] = int(row.get("responses_count") or 0)
    if row.get("address_text"):
        data["address_text"] = row.get("address_text")

    if row.get("location_lat") is not None and row.get("location_lon") is not None:
        data.setdefault("point", {"lat": float(row["location_lat"]), "lon": float(row["location_lon"])})
    else:
        fallback_point = primary_map_point(data) or destination_point(data)
        if fallback_point:
            data.setdefault("point", {"lat": float(fallback_point[0]), "lon": float(fallback_point[1])})

    description = (
        normalize_optional_text(row.get("description"))
        or normalize_optional_text(data.get("description"))
        or normalize_optional_text(data.get("generated_description"))
        or normalize_optional_text(normalize_json_object(data.get("search")).get("generated_description"))
        or normalize_optional_text(data.get("notes"))
        or normalize_optional_text(row.get("title"))
    )
    address_text = normalize_optional_text(row.get("address_text"), collapse_spaces=True) or primary_source_address(data)
    if description:
        data.setdefault("generated_description", description)
        data.setdefault("description", description)
        search = normalize_json_object(data.get("search"))
        search.setdefault("generated_description", description)
        data["search"] = search
    if address_text:
        data["address_text"] = address_text

    return {
        "id": str(row.get("id")),
        "user_id": str(row.get("customer_id")),
        "category": row.get("category_slug") or builder_category_slug(data.get("category")),
        "title": row.get("title") or "",
        "status": announcement_status,
        "description": description,
        "address_text": address_text,
        "data": data,
        "created_at": row.get("created_at"),
    }


def task_offer_row_to_legacy_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    status = canonical_offer_status_to_legacy(row.get("status"))
    if status == "rejected" and bool(row.get("can_reoffer")) is False:
        status = "blocked"

    proposed_price = row.get("proposed_price")
    if proposed_price is not None:
        proposed_price = int(round(float(proposed_price)))

    agreed_price = row.get("agreed_price")
    if agreed_price is not None:
        agreed_price = int(round(float(agreed_price)))

    return {
        "id": str(row.get("id")),
        "announcement_id": str(row.get("task_id")),
        "performer_id": str(row.get("performer_id")),
        "message": normalize_optional_text(row.get("message")),
        "proposed_price": proposed_price,
        "agreed_price": agreed_price,
        "pricing_mode": row.get("pricing_mode") or "counter_price",
        "minimum_price_accepted": bool(row.get("minimum_price_accepted")),
        "can_reoffer": bool(row.get("can_reoffer")),
        "status": status,
        "created_at": row.get("created_at"),
    }


def route_points_from_payload(task_id: str, data: Dict[str, Any]) -> list[Dict[str, Any]]:
    points: list[Dict[str, Any]] = []

    source = primary_map_point(data)
    if source:
        points.append(
            {
                "task_id": task_id,
                "point_order": 0,
                "title": "Старт",
                "address_text": primary_source_address(data),
                "point": source,
                "point_kind": "source",
            }
        )

    destination = destination_point(data)
    if destination:
        points.append(
            {
                "task_id": task_id,
                "point_order": 1,
                "title": "Финиш",
                "address_text": primary_destination_address(data),
                "point": destination,
                "point_kind": "destination",
            }
        )

    return points


def first_value(values: Iterable[Any]) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
