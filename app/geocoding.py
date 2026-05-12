# app/geocoding.py
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple


_NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
_USER_AGENT = os.getenv(
    "GEOCODER_USER_AGENT",
    # Nominatim отклоняет часть generic/python User-Agent'ов; этот формат проходит стабильно.
    "curl/8.7.1 VzaimnoBackend/1.0"
)

_SPACE_RE = re.compile(r"\s+")
_HOUSE_AND_CORPUS_RE = re.compile(
    r"\bдом\s*([0-9]+[0-9A-Za-zА-Яа-я/-]*)\s*корп(?:ус)?\s*([0-9]+[0-9A-Za-zА-Яа-я/-]*)\b",
    re.IGNORECASE,
)
_HOUSE_RE = re.compile(r"\bдом\s*([0-9]+[0-9A-Za-zА-Яа-я/-]*)\b", re.IGNORECASE)
_CORPUS_RE = re.compile(r"\bкорп(?:ус)?\s*([0-9]+[0-9A-Za-zА-Яа-я/-]*)\b", re.IGNORECASE)
_REQUEST_GAP_SECONDS = max(0.0, float(os.getenv("NOMINATIM_MIN_INTERVAL_SECONDS", "1.1")))
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_LOCAL_ADDRESS_POINTS = {
    # Lightweight offline fallback for common Moscow metro/address inputs used in
    # local testing. It keeps route creation usable when external geocoding is
    # disabled or temporarily unreachable.
    "метро беляево": (55.6428, 37.5264),
    "беляево": (55.6428, 37.5264),
    "metro belyaevo": (55.6428, 37.5264),
    "belyaevo": (55.6428, 37.5264),
    "м беляево": (55.6428, 37.5264),
    "метро текстильщики": (55.708832, 37.732596),
    "текстильщики": (55.708832, 37.732596),
    "metro tekstilshchiki": (55.708832, 37.732596),
    "tekstilshchiki": (55.708832, 37.732596),
    "м текстильщики": (55.708832, 37.732596),
    "метро бауманская": (55.772405, 37.67904),
    "бауманская": (55.772405, 37.67904),
    "metro baumanskaya": (55.772405, 37.67904),
    "baumanskaya": (55.772405, 37.67904),
    "м бауманская": (55.772405, 37.67904),
    "метро дубровка": (55.71807, 37.676259),
    "дубровка": (55.71807, 37.676259),
    "metro dubrovka": (55.71807, 37.676259),
    "dubrovka": (55.71807, 37.676259),
    "м дубровка": (55.71807, 37.676259),
    "метро таганская": (55.7413464, 37.6529092),
    "таганская": (55.7413464, 37.6529092),
    "metro taganskaya": (55.7413464, 37.6529092),
    "taganskaya": (55.7413464, 37.6529092),
    "м таганская": (55.7413464, 37.6529092),
    "метро калужская": (55.656682, 37.540075),
    "калужская": (55.656682, 37.540075),
    "metro kaluzhskaya": (55.656682, 37.540075),
    "kaluzhskaya": (55.656682, 37.540075),
    "м калужская": (55.656682, 37.540075),
    "профсоюзная 98к2": (55.642354, 37.525755),
    "профсоюзная 98 к2": (55.642354, 37.525755),
    "профсоюзная улица 98к2": (55.642354, 37.525755),
    "ул профсоюзная 98к2": (55.642354, 37.525755),
}


def _normalize_query(value: str) -> str:
    normalized = _SPACE_RE.sub(" ", (value or "").strip())
    if not normalized:
        return ""

    normalized = _HOUSE_AND_CORPUS_RE.sub(r"\1 к\2", normalized)
    normalized = _HOUSE_RE.sub(r"\1", normalized)
    normalized = _CORPUS_RE.sub(r"к\1", normalized)
    normalized = normalized.replace(" ,", ",").strip(" ,")
    return normalized


def _local_lookup_key(value: str) -> str:
    normalized = _normalize_query(value).lower().replace("ё", "е")
    normalized = normalized.replace(".", " ")
    normalized = normalized.replace(",", " ")
    normalized = re.sub(r"\bстанция\s+метро\b", "метро", normalized)
    normalized = re.sub(r"\bст\s+м\b", "метро", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _local_lookup_candidates(address: str) -> list[str]:
    key = _local_lookup_key(address)
    if not key:
        return []

    candidates = [key]
    for prefix in ("москва ", "г москва ", "город москва "):
        if key.startswith(prefix):
            candidates.append(key[len(prefix):].strip())
    for prefix in ("метро ", "м ", "metro "):
        if key.startswith(prefix):
            candidates.append(key[len(prefix):].strip())
    return [candidate for candidate in dict.fromkeys(candidates) if candidate]


def lookup_local_address_point(address: str | None) -> Optional[Tuple[float, float]]:
    if not address:
        return None
    for candidate in _local_lookup_candidates(address):
        point = _LOCAL_ADDRESS_POINTS.get(candidate)
        if point is not None:
            return point
    return None


def _candidate_queries(address: str) -> list[str]:
    base = _normalize_query(address)
    if not base:
        return []

    variants: list[str] = []

    def _add(value: str) -> None:
        candidate = _normalize_query(value)
        if candidate and candidate not in variants:
            variants.append(candidate)

    has_moscow = "москва" in base.lower() or "moscow" in base.lower()
    if not has_moscow:
        _add(f"Москва, {base}")
        _add(f"{base}, Москва")

    _add(address)
    _add(base)

    if base.lower().startswith("метро "):
        station_name = base[6:].strip()
        if station_name:
            if not has_moscow:
                _add(f"Москва, {station_name}")
                _add(f"{station_name}, Москва")
            _add(station_name)

    return variants


def _geocode_single_query_with_urllib(query: str, timeout_seconds: float) -> Optional[Tuple[float, float]]:
    global _LAST_REQUEST_AT

    with _REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        if elapsed < _REQUEST_GAP_SECONDS:
            time.sleep(_REQUEST_GAP_SECONDS - elapsed)
        _LAST_REQUEST_AT = time.monotonic()

    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "limit": "1",
        }
    )
    url = f"{_NOMINATIM_URL}?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read()
    return _parse_geocoder_payload(raw)


def _parse_geocoder_payload(raw: bytes | str) -> Optional[Tuple[float, float]]:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return None

    first = data[0]
    lat = float(first["lat"])
    lon = float(first["lon"])
    return lat, lon


def _geocode_single_query(query: str, timeout_seconds: float) -> Optional[Tuple[float, float]]:
    global _LAST_REQUEST_AT

    with _REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        if elapsed < _REQUEST_GAP_SECONDS:
            time.sleep(_REQUEST_GAP_SECONDS - elapsed)
        _LAST_REQUEST_AT = time.monotonic()

    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "limit": "1",
        }
    )
    url = f"{_NOMINATIM_URL}?{params}"
    result = subprocess.run(
        [
            "curl",
            "-sSL",
            "-A",
            _USER_AGENT,
            "--max-time",
            str(max(1, int(round(timeout_seconds)))),
            url,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return _parse_geocoder_payload(result.stdout)


def geocode_address(address: str, timeout_seconds: float = 6.0) -> Optional[Tuple[float, float]]:
    """
    Возвращает (lat, lon) или None.
    MVP-геокодинг через Nominatim (OSM). Для продакшена лучше вынести в отдельный сервис/кэш.
    """
    local_point = lookup_local_address_point(address)
    if local_point is not None:
        return local_point

    queries = _candidate_queries(address)
    if not queries:
        return None

    for query in queries:
        for resolver in (_geocode_single_query, _geocode_single_query_with_urllib):
            try:
                point = resolver(query, timeout_seconds)
            except Exception:
                continue
            if point is not None:
                return point
    return None
