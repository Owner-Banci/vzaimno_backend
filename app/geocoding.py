# app/geocoding.py
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Optional, Tuple


_NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
_USER_AGENT = os.getenv(
    "GEOCODER_USER_AGENT",
    # По правилам Nominatim желательно указывать понятный User-Agent
    "slayma-mvp/1.0 (contact: you@example.com)"
)


def geocode_address(address: str, timeout_seconds: float = 6.0) -> Optional[Tuple[float, float]]:
    """
    Возвращает (lat, lon) или None.
    MVP-геокодинг через Nominatim (OSM). Для продакшена лучше вынести в отдельный сервис/кэш.
    """
    q = (address or "").strip()
    if not q:
        return None

    params = urllib.parse.urlencode(
        {
            "q": q,
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

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list) or not data:
            return None

        first = data[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        return lat, lon
    except Exception:
        return None