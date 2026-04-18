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


def _normalize_query(value: str) -> str:
    normalized = _SPACE_RE.sub(" ", (value or "").strip())
    if not normalized:
        return ""

    normalized = _HOUSE_AND_CORPUS_RE.sub(r"\1 к\2", normalized)
    normalized = _HOUSE_RE.sub(r"\1", normalized)
    normalized = _CORPUS_RE.sub(r"к\1", normalized)
    normalized = normalized.replace(" ,", ",").strip(" ,")
    return normalized


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
