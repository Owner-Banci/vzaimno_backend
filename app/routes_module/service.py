from __future__ import annotations

import json
import math
from typing import Any

from fastapi import HTTPException

from app.config import get_env, get_int
from app.db import fetch_all, fetch_one
from app.geocoding import geocode_address
from app.storage import default_presigned_expires_seconds, get_storage
from app.task_compat import ensure_task_payload

from .schemas import CoordinateOut, RouteContextOut, RouteDetailsOut, RouteTaskByPathOut
from .sql import (
    FIND_CURRENT_ROUTE_TASK_SQL,
    FIND_KNOWN_ROUTE_POINT_BY_ADDRESS_SQL,
    FIND_TASK_ROUTE_POINTS_SQL,
    FIND_TASK_ROUTE_CONTEXT_SQL,
    NEARBY_TASKS_BY_ROUTE_SQL,
)

EARTH_RADIUS_M = 6_371_008.8
DEFAULT_ROUTE_RADIUS_METERS = max(50, get_int("ROUTE_TASK_RADIUS_METERS", 500))
DEFAULT_ROUTE_LIMIT = max(1, get_int("ROUTE_TASKS_LIMIT", 50))
DEFAULT_TRAVEL_MODE = get_env("ROUTE_DEFAULT_TRAVEL_MODE", "driving") or "driving"
SUPPORTED_TRAVEL_MODES = {"driving", "walking", "truck", "transit", "bicycle", "scooter"}


def build_route_for_current_user(
    user_id: str,
    *,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
    limit: int = DEFAULT_ROUTE_LIMIT,
) -> RouteDetailsOut:
    announcement_id = _resolve_current_announcement_id(user_id)
    return build_route_for_announcement(
        announcement_id=announcement_id,
        user_id=user_id,
        radius_m=radius_m,
        limit=limit,
    )


def build_route_for_announcement(
    announcement_id: str,
    user_id: str,
    *,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
    limit: int = DEFAULT_ROUTE_LIMIT,
) -> RouteDetailsOut:
    context = build_route_context_for_announcement(
        announcement_id=announcement_id,
        user_id=user_id,
        radius_m=radius_m,
    )
    return build_route_from_polyline(
        announcement_id=announcement_id,
        user_id=user_id,
        polyline=[
            [context.start.lat, context.start.lon],
            [context.end.lat, context.end.lon],
        ],
        start_address=context.start_address,
        end_address=context.end_address,
        distance_meters=None,
        duration_seconds=None,
        radius_m=radius_m,
        limit=limit,
        travel_mode=context.travel_mode,
    )


def build_route_context_for_current_user(
    user_id: str,
    *,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
) -> RouteContextOut:
    announcement_id = _resolve_current_announcement_id(user_id)
    return build_route_context_for_announcement(
        announcement_id=announcement_id,
        user_id=user_id,
        radius_m=radius_m,
    )


def build_route_context_for_announcement(
    announcement_id: str,
    user_id: str,
    *,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
) -> RouteContextOut:
    context = _load_route_context(announcement_id=announcement_id, user_id=user_id)
    start_point = context["start_point"]
    end_point = context["end_point"]

    return RouteContextOut(
        entity_id=context["announcement_id"],
        start_address=context["start_address"],
        end_address=context["end_address"],
        start=CoordinateOut(lat=start_point[0], lon=start_point[1]),
        end=CoordinateOut(lat=end_point[0], lon=end_point[1]),
        radius_m=max(50, int(radius_m)),
        travel_mode=context["travel_mode"],
    )


def build_route_from_polyline(
    *,
    announcement_id: str | None,
    user_id: str,
    polyline: list[list[float]],
    start_address: str | None,
    end_address: str | None,
    distance_meters: int | None,
    duration_seconds: int | None,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
    limit: int = DEFAULT_ROUTE_LIMIT,
    travel_mode: str = DEFAULT_TRAVEL_MODE,
) -> RouteDetailsOut:
    resolved_announcement_id = announcement_id or _resolve_current_announcement_id(user_id)
    context = _load_route_context(announcement_id=resolved_announcement_id, user_id=user_id)

    normalized_polyline = _parse_input_polyline(polyline)
    if len(normalized_polyline) < 2:
        raise HTTPException(status_code=422, detail="Polyline маршрута должна содержать минимум 2 точки")

    normalized_mode = _normalize_travel_mode(travel_mode)
    effective_distance = (
        int(distance_meters)
        if distance_meters is not None and int(distance_meters) > 0
        else int(round(_polyline_length_meters(normalized_polyline)))
    )
    effective_duration = (
        int(duration_seconds)
        if duration_seconds is not None and int(duration_seconds) > 0
        else _estimate_duration_seconds(effective_distance, normalized_mode)
    )

    resolved_start_address = _normalize_address(start_address) or context["start_address"]
    resolved_end_address = _normalize_address(end_address) or context["end_address"]
    tasks_by_route = _fetch_tasks_by_route(
        current_announcement_id=context["announcement_id"],
        current_user_id=user_id,
        route_polyline=normalized_polyline,
        radius_m=radius_m,
        limit=limit,
    )

    return RouteDetailsOut(
        entity_id=context["announcement_id"],
        start_address=resolved_start_address,
        end_address=resolved_end_address,
        distance_meters=effective_distance,
        duration_seconds=effective_duration,
        distance_text=_format_distance(effective_distance),
        duration_text=_format_duration(effective_duration),
        polyline=normalized_polyline,
        tasks_by_route=tasks_by_route,
    )


def _resolve_current_announcement_id(user_id: str) -> str:
    row = fetch_one(FIND_CURRENT_ROUTE_TASK_SQL, (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Для вас пока нет активного маршрута")
    return str(row[0])


def _load_route_context(*, announcement_id: str, user_id: str) -> dict[str, Any]:
    row = fetch_one(FIND_TASK_ROUTE_CONTEXT_SQL, (announcement_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Задание не найдено")

    ann_id = str(row[0])
    ann_owner_id = str(row[1])
    category = str(row[2] or "")
    title = str(row[3] or "")
    raw_data = _coerce_data(row[4])
    if row[5] and "address_text" not in raw_data:
        raw_data["address_text"] = str(row[5])
    if row[6] is not None and row[7] is not None and "point" not in raw_data:
        raw_data["point"] = {"lat": float(row[6]), "lon": float(row[7])}
    performer_id = str(row[8]) if row[8] else None
    assignment_status = str(row[9] or "")
    execution_stage = str(row[10] or "")
    route_visibility = str(row[11] or "")
    data = ensure_task_payload(
        raw_data,
        title=title,
        announcement_status=_route_announcement_status(
            assignment_status=assignment_status,
            execution_stage=execution_stage,
        ),
        assignment={
            "performer_id": performer_id,
            "assignment_status": assignment_status,
            "execution_stage": execution_stage,
            "route_visibility": route_visibility,
        },
    )

    _assert_route_access(
        owner_id=ann_owner_id,
        performer_id=performer_id,
        assignment_status=assignment_status,
        user_id=user_id,
    )

    if assignment_status not in {"assigned", "in_progress"}:
        raise HTTPException(status_code=409, detail="Маршрут появится после принятия активного задания")

    route_points = _extract_route_points(data, category)
    stored_route_points = _load_stored_route_points(ann_id)
    if not route_points:
        route_points = _route_points_from_stored(stored_route_points)
    if not route_points:
        raise HTTPException(
            status_code=422,
            detail="Не удалось определить старт и финиш маршрута. Проверьте адреса/координаты объявления.",
        )

    start_point, end_point = route_points
    start_address, end_address = _resolve_route_addresses(
        data,
        category,
        start_point,
        end_point,
        stored_route_points=stored_route_points,
    )

    return {
        "announcement_id": ann_id,
        "category": category,
        "data": data,
        "start_point": start_point,
        "end_point": end_point,
        "start_address": start_address,
        "end_address": end_address,
        "travel_mode": _task_travel_mode(data),
    }


def _assert_route_access(
    *,
    owner_id: str,
    performer_id: str | None,
    assignment_status: str,
    user_id: str,
) -> None:
    if owner_id == user_id:
        return

    if performer_id == user_id and assignment_status in {"assigned", "in_progress"}:
        return

    raise HTTPException(status_code=403, detail="У вас нет доступа к маршруту этого объявления")


def _route_announcement_status(*, assignment_status: str, execution_stage: str) -> str:
    normalized_assignment = (assignment_status or "").strip().lower()
    normalized_stage = (execution_stage or "").strip().lower()

    if normalized_stage in {"en_route", "on_site", "in_progress", "handoff"}:
        return "in_progress"
    if normalized_assignment == "assigned":
        return "assigned"
    if normalized_assignment == "in_progress":
        return "in_progress"
    return "active"


def _task_travel_mode(data: dict[str, Any]) -> str:
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    route = task.get("route") if isinstance(task.get("route"), dict) else {}
    return _normalize_travel_mode(route.get("travel_mode") or data.get("travel_mode") or DEFAULT_TRAVEL_MODE)


def _coerce_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _parse_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _extract_point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    lat = _first_float(value.get("lat"), value.get("latitude"))
    lon = _first_float(value.get("lon"), value.get("lng"), value.get("longitude"))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _extract_flat_point(data: dict[str, Any], prefixes: tuple[str, ...]) -> tuple[float, float] | None:
    for prefix in prefixes:
        lat = _first_float(data.get(f"{prefix}_lat"), data.get(f"{prefix}_latitude"))
        lon = _first_float(
            data.get(f"{prefix}_lon"),
            data.get(f"{prefix}_lng"),
            data.get(f"{prefix}_longitude"),
        )
        if lat is None or lon is None:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def _normalize_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _parse_float(value)
        if parsed is not None:
            return parsed
    return None


def _extract_route_points(
    data: dict[str, Any],
    category: str,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    normalized_category = category.strip().lower()
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    route = task.get("route") if isinstance(task.get("route"), dict) else {}
    route_source = route.get("source") if isinstance(route.get("source"), dict) else {}
    route_destination = route.get("destination") if isinstance(route.get("destination"), dict) else {}

    start = (
        _extract_point(data.get("pickup_point"))
        or _extract_point(data.get("help_point"))
        or _extract_point(data.get("start_point"))
        or _extract_point(data.get("source_point"))
        or _extract_point(data.get("from_point"))
        or _extract_point(data.get("origin_point"))
        or _extract_point(data.get("point"))
        or _extract_point(route_source.get("point"))
        or _extract_flat_point(data, ("pickup", "help", "start", "source", "from", "origin"))
    )
    end = (
        _extract_point(data.get("dropoff_point"))
        or _extract_point(data.get("end_point"))
        or _extract_point(data.get("destination_point"))
        or _extract_point(data.get("to_point"))
        or _extract_point(data.get("delivery_point"))
        or _extract_point(route_destination.get("point"))
        or _extract_flat_point(data, ("dropoff", "end", "destination", "to", "delivery"))
    )

    start_address = (
        _normalize_address(data.get("pickup_address"))
        or _normalize_address(data.get("address"))
        or _normalize_address(data.get("start_address"))
        or _normalize_address(data.get("address_text"))
        or _normalize_address(route_source.get("address"))
    )
    end_address = (
        _normalize_address(data.get("dropoff_address"))
        or _normalize_address(data.get("end_address"))
        or _normalize_address(data.get("to_address"))
        or _normalize_address(data.get("destination_address"))
        or _normalize_address(route_destination.get("address"))
    )

    if normalized_category == "delivery":
        if start is None and start_address:
            start = _resolve_address_point(start_address)
        if end is None and end_address:
            end = _resolve_address_point(end_address)
    else:
        if start is None and start_address:
            start = _resolve_address_point(start_address)
        if end is None and end_address:
            end = _resolve_address_point(end_address)

    if start is not None and end is None:
        end = start
    elif end is not None and start is None:
        start = end

    if start is None or end is None:
        return None
    return start, end


def _resolve_address_point(address: str) -> tuple[float, float] | None:
    known_point = _lookup_known_address_point(address)
    if known_point is not None:
        return known_point
    return geocode_address(address)


def _lookup_known_address_point(address: str) -> tuple[float, float] | None:
    normalized = _normalize_address(address)
    if not normalized:
        return None
    row = fetch_one(FIND_KNOWN_ROUTE_POINT_BY_ADDRESS_SQL, (normalized,))
    if not row:
        return None
    lat = _parse_float(row[0])
    lon = _parse_float(row[1])
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _load_stored_route_points(announcement_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(FIND_TASK_ROUTE_POINTS_SQL, (announcement_id,))
    points: list[dict[str, Any]] = []
    for row in rows:
        lat = _parse_float(row[3])
        lon = _parse_float(row[4])
        if lat is None or lon is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        points.append(
            {
                "order": int(row[0] or 0),
                "address": _normalize_address(row[1]),
                "kind": _normalize_address(row[2]),
                "point": (lat, lon),
            }
        )
    return points


def _route_points_from_stored(
    stored_route_points: list[dict[str, Any]],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not stored_route_points:
        return None
    if len(stored_route_points) == 1:
        point = stored_route_points[0]["point"]
        return point, point
    return stored_route_points[0]["point"], stored_route_points[-1]["point"]


def _resolve_route_addresses(
    data: dict[str, Any],
    category: str,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    *,
    stored_route_points: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    category_key = category.strip().lower()
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    route = task.get("route") if isinstance(task.get("route"), dict) else {}
    route_source = route.get("source") if isinstance(route.get("source"), dict) else {}
    route_destination = route.get("destination") if isinstance(route.get("destination"), dict) else {}
    if category_key == "delivery":
        start_address = _normalize_address(data.get("pickup_address")) or _normalize_address(route_source.get("address"))
        end_address = _normalize_address(data.get("dropoff_address")) or _normalize_address(route_destination.get("address"))
    else:
        start_address = (
            _normalize_address(data.get("start_address"))
            or _normalize_address(data.get("address"))
            or _normalize_address(data.get("address_text"))
            or _normalize_address(route_source.get("address"))
        )
        end_address = (
            _normalize_address(data.get("end_address"))
            or _normalize_address(data.get("to_address"))
            or _normalize_address(data.get("destination_address"))
            or _normalize_address(route_destination.get("address"))
        )

    if not start_address:
        start_address = _stored_route_address(stored_route_points, first=True)
    if not end_address:
        end_address = _stored_route_address(stored_route_points, first=False)

    if not start_address:
        start_address = _format_point(start_point)
    if not end_address:
        end_address = _format_point(end_point)

    return start_address, end_address


def _stored_route_address(stored_route_points: list[dict[str, Any]] | None, *, first: bool) -> str | None:
    if not stored_route_points:
        return None
    point = stored_route_points[0] if first else stored_route_points[-1]
    return _normalize_address(point.get("address"))


def _format_point(point: tuple[float, float]) -> str:
    return f"{point[0]:.6f}, {point[1]:.6f}"


def _normalize_travel_mode(value: str | None) -> str:
    normalized = (value or DEFAULT_TRAVEL_MODE).strip().lower()
    aliases = {
        "pedestrian": "walking",
        "foot": "walking",
        "car": "driving",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_TRAVEL_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Неподдерживаемый travel_mode: {value!r}. "
                f"Поддерживаются: {', '.join(sorted(SUPPORTED_TRAVEL_MODES))}"
            ),
        )
    return normalized


def _parse_input_polyline(raw_polyline: list[list[float]]) -> list[list[float]]:
    polyline: list[list[float]] = []
    for raw_pair in raw_polyline:
        parsed = _parse_lat_lon_pair(raw_pair)
        if parsed is not None:
            polyline.append([parsed[0], parsed[1]])
    return polyline


def _parse_lat_lon_pair(raw_pair: Any) -> tuple[float, float] | None:
    if not isinstance(raw_pair, (list, tuple)) or len(raw_pair) < 2:
        return None

    first = _parse_float(raw_pair[0])
    second = _parse_float(raw_pair[1])
    if first is None or second is None:
        return None

    lat, lon = first, second
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat, lon = second, first

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _polyline_length_meters(polyline: list[list[float]]) -> float:
    if len(polyline) < 2:
        return 0.0
    return sum(
        _haversine_distance_meters(tuple(start), tuple(end))
        for start, end in zip(polyline, polyline[1:])
    )


def _haversine_distance_meters(
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    lat1 = math.radians(start[0])
    lat2 = math.radians(end[0])
    d_lat = lat2 - lat1
    d_lon = math.radians(end[1] - start[1])

    sin_lat = math.sin(d_lat / 2.0)
    sin_lon = math.sin(d_lon / 2.0)
    a = sin_lat * sin_lat + math.cos(lat1) * math.cos(lat2) * sin_lon * sin_lon
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _estimate_duration_seconds(distance_meters: int, travel_mode: str) -> int:
    speeds = {
        "walking": 1.4,
        "bicycle": 4.5,
        "scooter": 5.5,
        "transit": 6.0,
        "driving": 8.0,
        "truck": 7.0,
    }
    speed = speeds.get(travel_mode, 6.0)
    return max(1, int(round(distance_meters / max(0.1, speed))))


def _fetch_tasks_by_route(
    *,
    current_announcement_id: str,
    current_user_id: str,
    route_polyline: list[list[float]],
    radius_m: int,
    limit: int,
) -> list[RouteTaskByPathOut]:
    route_geojson = {
        "type": "LineString",
        "coordinates": [[point[1], point[0]] for point in route_polyline],
    }
    rows = fetch_all(
        NEARBY_TASKS_BY_ROUTE_SQL,
        (
            json.dumps(route_geojson, ensure_ascii=False),
            current_announcement_id,
            current_user_id,
            int(radius_m),
            int(limit),
        ),
    )

    result: list[RouteTaskByPathOut] = []
    for row in rows:
        data = _coerce_data(row[4])
        result.append(
            RouteTaskByPathOut(
                id=str(row[0]),
                title=str(row[1] or "Без названия"),
                category=_normalize_address(row[2]),
                status=_normalize_address(row[3]),
                address_text=_extract_address_text(data, str(row[2] or "")),
                latitude=_parse_float(row[5]),
                longitude=_parse_float(row[6]),
                distance_to_route_meters=float(row[7] or 0),
                price_text=_extract_price_text(data),
                preview_image_url=_extract_preview_image_url(data),
            )
        )
    return result


def _extract_address_text(data: dict[str, Any], category: str) -> str | None:
    if category.strip().lower() == "delivery":
        pickup = _normalize_address(data.get("pickup_address"))
        dropoff = _normalize_address(data.get("dropoff_address"))
        if pickup and dropoff:
            return f"{pickup} → {dropoff}"
        return pickup or dropoff or _normalize_address(data.get("address_text"))
    return (
        _normalize_address(data.get("address"))
        or _normalize_address(data.get("address_text"))
        or _normalize_address(data.get("pickup_address"))
    )


def _extract_price_text(data: dict[str, Any]) -> str | None:
    budget_min = _to_int(data.get("budget_min"))
    budget_max = _to_int(data.get("budget_max"))
    budget = _to_int(data.get("budget"))

    if budget_min is not None and budget_max is not None:
        if budget_min == budget_max:
            return _format_price(budget_min)
        return f"{_format_price_raw(budget_min)}–{_format_price_raw(budget_max)} ₽"
    if budget_min is not None:
        return f"от {_format_price_raw(budget_min)} ₽"
    if budget_max is not None:
        return f"до {_format_price_raw(budget_max)} ₽"
    if budget is not None:
        return _format_price(budget)
    return None


def _format_price(value: int) -> str:
    return f"{_format_price_raw(value)} ₽"


def _format_price_raw(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _to_int(value: Any) -> int | None:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _extract_preview_image_url(data: dict[str, Any]) -> str | None:
    media_candidates: list[Any] = []

    for key in ("media", "images", "photos"):
        raw_value = data.get(key)
        if isinstance(raw_value, list):
            media_candidates.extend(raw_value)
        elif raw_value is not None:
            media_candidates.append(raw_value)

    for candidate in media_candidates:
        url = _extract_media_url(candidate)
        if url:
            return url
    return None


def _extract_media_url(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None

    if isinstance(value, dict):
        object_key = value.get("object_key")
        if isinstance(object_key, str) and object_key.strip():
            normalized_key = object_key.strip().lstrip("/")
            try:
                return get_storage().get_url(
                    normalized_key,
                    expires_seconds=default_presigned_expires_seconds(),
                )
            except Exception:
                return f"/uploads/{normalized_key}"

        for key in (
            "preview_url",
            "previewUrl",
            "thumbnail_url",
            "thumbnailUrl",
            "url",
            "image_url",
            "imageUrl",
            "file_url",
            "fileUrl",
            "path",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        nested = value.get("file")
        return _extract_media_url(nested)

    return None


def _format_distance(distance_meters: int) -> str:
    value = max(0, int(distance_meters))
    if value < 1000:
        return f"{value} м"
    km = value / 1000.0
    return f"{km:.1f} км"


def _format_duration(duration_seconds: int) -> str:
    seconds = max(0, int(duration_seconds))
    minutes = max(1, int(round(seconds / 60.0)))

    if minutes < 60:
        return f"{minutes} мин"

    hours = minutes // 60
    left_minutes = minutes % 60
    if left_minutes == 0:
        return f"{hours} ч"
    return f"{hours} ч {left_minutes} мин"
