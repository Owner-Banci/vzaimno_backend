from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.db import fetch_all, fetch_one
from app.geocoding import geocode_address

from .schemas import RouteDetailsOut, RouteTaskByPathOut
from .sql import (
    FIND_ANNOUNCEMENT_SQL,
    FIND_CURRENT_ROUTE_ANNOUNCEMENT_SQL,
    HAS_ACCEPTED_OFFER_SQL,
    NEARBY_TASKS_BY_ROUTE_SQL,
)

YANDEX_ROUTING_API_URL = os.getenv("YANDEX_ROUTING_API_URL", "https://api.routing.yandex.net/v2/route")
YANDEX_ROUTING_TIMEOUT = float(os.getenv("YANDEX_ROUTING_TIMEOUT", "12"))
DEFAULT_ROUTE_RADIUS_METERS = int(os.getenv("ROUTE_TASK_RADIUS_METERS", "500"))
DEFAULT_ROUTE_LIMIT = int(os.getenv("ROUTE_TASKS_LIMIT", "50"))


def build_route_for_current_user(
    user_id: str,
    *,
    radius_m: int = DEFAULT_ROUTE_RADIUS_METERS,
    limit: int = DEFAULT_ROUTE_LIMIT,
) -> RouteDetailsOut:
    row = fetch_one(FIND_CURRENT_ROUTE_ANNOUNCEMENT_SQL, (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Для вас пока нет активного маршрута")

    return build_route_for_announcement(
        announcement_id=str(row[0]),
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
    row = fetch_one(FIND_ANNOUNCEMENT_SQL, (announcement_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Объявление не найдено")

    ann_id = str(row[0])
    ann_owner_id = str(row[1])
    category = str(row[2] or "")
    status = str(row[4] or "")
    data = _coerce_data(row[5])

    _assert_route_access(
        announcement_id=ann_id,
        owner_id=ann_owner_id,
        user_id=user_id,
    )

    if status != "active":
        raise HTTPException(status_code=409, detail="Маршрут доступен только для активного объявления")

    route_points = _extract_route_points(data, category)
    if not route_points:
        raise HTTPException(
            status_code=422,
            detail="Не удалось определить старт и финиш маршрута. Проверьте адреса/координаты объявления.",
        )

    start_point, end_point = route_points
    start_address, end_address = _resolve_route_addresses(data, category, start_point, end_point)

    polyline, distance_meters, duration_seconds = _request_yandex_route(start_point, end_point)
    if len(polyline) < 2:
        raise HTTPException(status_code=502, detail="Яндекс не вернул геометрию маршрута")

    tasks_by_route = _fetch_tasks_by_route(
        current_announcement_id=ann_id,
        current_user_id=user_id,
        route_polyline=polyline,
        radius_m=radius_m,
        limit=limit,
    )

    return RouteDetailsOut(
        entity_id=ann_id,
        start_address=start_address,
        end_address=end_address,
        distance_meters=distance_meters,
        duration_seconds=duration_seconds,
        distance_text=_format_distance(distance_meters),
        duration_text=_format_duration(duration_seconds),
        polyline=polyline,
        tasks_by_route=tasks_by_route,
    )


def _assert_route_access(announcement_id: str, owner_id: str, user_id: str) -> None:
    if owner_id == user_id:
        return

    offer = fetch_one(HAS_ACCEPTED_OFFER_SQL, (announcement_id, user_id))
    if offer:
        return

    raise HTTPException(status_code=403, detail="У вас нет доступа к маршруту этого объявления")


def _coerce_data(value: Any) -> Dict[str, Any]:
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


def _parse_float(value: Any) -> Optional[float]:
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


def _extract_point(value: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(value, dict):
        return None
    lat = _parse_float(value.get("lat"))
    lon = _parse_float(value.get("lon"))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _normalize_address(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _extract_route_points(
    data: Dict[str, Any],
    category: str,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    normalized_category = category.strip().lower()

    start = (
        _extract_point(data.get("pickup_point"))
        or _extract_point(data.get("help_point"))
        or _extract_point(data.get("start_point"))
        or _extract_point(data.get("point"))
    )
    end = (
        _extract_point(data.get("dropoff_point"))
        or _extract_point(data.get("end_point"))
        or _extract_point(data.get("destination_point"))
        or _extract_point(data.get("to_point"))
    )

    start_address = (
        _normalize_address(data.get("pickup_address"))
        or _normalize_address(data.get("address"))
        or _normalize_address(data.get("start_address"))
        or _normalize_address(data.get("address_text"))
    )
    end_address = (
        _normalize_address(data.get("dropoff_address"))
        or _normalize_address(data.get("end_address"))
        or _normalize_address(data.get("to_address"))
        or _normalize_address(data.get("destination_address"))
    )

    if normalized_category == "delivery":
        if start is None and start_address:
            start = geocode_address(start_address)
        if end is None and end_address:
            end = geocode_address(end_address)
    else:
        if start is None and start_address:
            start = geocode_address(start_address)
        if end is None and end_address:
            end = geocode_address(end_address)

    if start is None or end is None:
        return None
    return start, end


def _resolve_route_addresses(
    data: Dict[str, Any],
    category: str,
    start_point: Tuple[float, float],
    end_point: Tuple[float, float],
) -> Tuple[str, str]:
    category_key = category.strip().lower()
    if category_key == "delivery":
        start_address = _normalize_address(data.get("pickup_address"))
        end_address = _normalize_address(data.get("dropoff_address"))
    else:
        start_address = (
            _normalize_address(data.get("start_address"))
            or _normalize_address(data.get("address"))
            or _normalize_address(data.get("address_text"))
        )
        end_address = (
            _normalize_address(data.get("end_address"))
            or _normalize_address(data.get("to_address"))
            or _normalize_address(data.get("destination_address"))
        )

    if not start_address:
        start_address = _format_point(start_point)
    if not end_address:
        end_address = _format_point(end_point)

    return start_address, end_address


def _format_point(point: Tuple[float, float]) -> str:
    return f"{point[0]:.6f}, {point[1]:.6f}"


def _request_yandex_route(
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> Tuple[List[List[float]], int, int]:
    api_key = _resolve_routing_key()

    query = urllib.parse.urlencode(
        {
            "apikey": api_key,
            "waypoints": f"{start[0]},{start[1]}|{end[0]},{end[1]}",
            "mode": "driving",
            "lang": "ru_RU",
        }
    )
    url = f"{YANDEX_ROUTING_API_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "vzaimno-backend/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=YANDEX_ROUTING_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=502, detail=f"Yandex Routing HTTP {exc.code}: {body[:240]}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить маршрут от Yandex: {exc!s}")

    route = _extract_first_route(payload)
    if not route:
        raise HTTPException(status_code=502, detail="Yandex Routing не вернул ни одного маршрута")
    polyline = _extract_route_polyline(route)
    distance_meters = _extract_route_distance(route)
    duration_seconds = _extract_route_duration(route)

    if distance_meters <= 0:
        distance_meters = _sum_route_lengths(route)
    if duration_seconds <= 0:
        duration_seconds = _sum_route_durations(route)

    return polyline, max(0, distance_meters), max(0, duration_seconds)


def _extract_route_polyline(route: Dict[str, Any]) -> List[List[float]]:
    points: List[List[float]] = []
    _append_polyline_points(points, route.get("polyline"))

    legs = route.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            _append_polyline_points(points, leg.get("polyline"))
            steps = leg.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        _append_polyline_points(points, step.get("polyline"))

    normalized: List[List[float]] = []
    for item in points:
        if not normalized or normalized[-1] != item:
            normalized.append(item)
    return normalized


def _append_polyline_points(target: List[List[float]], polyline_value: Any) -> None:
    if not isinstance(polyline_value, dict):
        return
    raw_points = polyline_value.get("points")
    if not isinstance(raw_points, list):
        return

    for raw_pair in raw_points:
        parsed = _parse_lat_lon_pair(raw_pair)
        if parsed is None:
            continue
        target.append([parsed[0], parsed[1]])


def _parse_lat_lon_pair(raw_pair: Any) -> Optional[Tuple[float, float]]:
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


def _extract_route_distance(route: Dict[str, Any]) -> int:
    summary = route.get("summary")
    if isinstance(summary, dict):
        length = _parse_float(summary.get("length"))
        if length is not None:
            return int(round(length))
    properties = route.get("properties")
    if isinstance(properties, dict):
        length = _parse_float(properties.get("length"))
        if length is not None:
            return int(round(length))
    return 0


def _extract_route_duration(route: Dict[str, Any]) -> int:
    summary = route.get("summary")
    if isinstance(summary, dict):
        duration_value = _coerce_duration_seconds(summary.get("duration"))
        if duration_value is not None:
            return int(round(duration_value))
    properties = route.get("properties")
    if isinstance(properties, dict):
        duration_value = _coerce_duration_seconds(properties.get("duration"))
        if duration_value is not None:
            return int(round(duration_value))
    return 0


def _sum_route_lengths(route: Dict[str, Any]) -> int:
    total = 0.0
    legs = route.get("legs")
    if not isinstance(legs, list):
        return 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        summary = leg.get("summary")
        if isinstance(summary, dict):
            total += _parse_float(summary.get("length")) or 0.0
    return int(round(total))


def _sum_route_durations(route: Dict[str, Any]) -> int:
    total = 0.0
    legs = route.get("legs")
    if not isinstance(legs, list):
        return 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        summary = leg.get("summary")
        if not isinstance(summary, dict):
            continue
        duration_value = _coerce_duration_seconds(summary.get("duration"))
        if duration_value is not None:
            total += duration_value
            continue

        steps = leg.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_duration = _coerce_duration_seconds(step.get("duration"))
            if step_duration is not None:
                total += step_duration
    return int(round(total))


def _extract_first_route(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    routes = payload.get("routes")
    if isinstance(routes, list) and routes:
        first = routes[0]
        if isinstance(first, dict):
            return first

    route = payload.get("route")
    if isinstance(route, dict):
        return route

    result = payload.get("result")
    if isinstance(result, dict):
        nested_routes = result.get("routes")
        if isinstance(nested_routes, list) and nested_routes and isinstance(nested_routes[0], dict):
            return nested_routes[0]
        nested_route = result.get("route")
        if isinstance(nested_route, dict):
            return nested_route

    return None


def _coerce_duration_seconds(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        return _parse_float(value.get("value")) or _parse_float(value.get("seconds"))
    return _parse_float(value)


def _resolve_routing_key() -> str:
    key = (
        os.getenv("YANDEX_ROUTING_API_KEY")
        or os.getenv("YANDEX_MAPKIT_API_KEY")
        or os.getenv("YANDEX_API_KEY")
    )
    if not key:
        raise HTTPException(
            status_code=503,
            detail="Не настроен ключ Yandex Routing API (YANDEX_ROUTING_API_KEY).",
        )
    return key


def _fetch_tasks_by_route(
    *,
    current_announcement_id: str,
    current_user_id: str,
    route_polyline: List[List[float]],
    radius_m: int,
    limit: int,
) -> List[RouteTaskByPathOut]:
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

    result: List[RouteTaskByPathOut] = []
    for row in rows:
        data = _coerce_data(row[4])
        result.append(
            RouteTaskByPathOut(
                id=str(row[0]),
                title=str(row[1] or "Без названия"),
                category=_normalize_address(row[2]),
                status=_normalize_address(row[3]),
                address_text=_extract_address_text(data, str(row[2] or "")),
                distance_to_route_meters=float(row[5] or 0),
                price_text=_extract_price_text(data),
                preview_image_url=_extract_preview_image_url(data),
            )
        )
    return result


def _extract_address_text(data: Dict[str, Any], category: str) -> Optional[str]:
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


def _extract_price_text(data: Dict[str, Any]) -> Optional[str]:
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


def _to_int(value: Any) -> Optional[int]:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _extract_preview_image_url(data: Dict[str, Any]) -> Optional[str]:
    media_candidates: List[Any] = []

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


def _extract_media_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None

    if isinstance(value, dict):
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
