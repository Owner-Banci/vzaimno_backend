# app/moderation_text.py
from __future__ import annotations

import json
import time
import urllib.request
from functools import lru_cache
from typing import Any, Dict, Optional

from app.config import get_env, get_float, get_int
from app.external import call_external_sync


OLLAMA_URL = get_env("OLLAMA_URL", "http://localhost:11434/api/generate") or "http://localhost:11434/api/generate"
OLLAMA_MODEL = get_env("OLLAMA_MODEL", "shieldgemma:2b") or "shieldgemma:2b"
OLLAMA_TIMEOUT = max(0.5, get_float("OLLAMA_TIMEOUT_S", get_float("OLLAMA_TIMEOUT", 15.0)))
OLLAMA_RETRIES = max(0, get_int("OLLAMA_RETRIES", 1))

SYSTEM = (
    "You are a legality classification model. "
    "Classify the text into LEGAL or ILLEGAL. "
    "Return ONLY JSON: {\"label\":\"LEGAL|ILLEGAL\",\"reason\":\"...\"}."
)

def _to_json_or_none(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            return None
    # иногда модель может “обрамлять” JSON — вытаскиваем по скобкам
    i = text.find("{")
    j = text.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(text[i : j + 1])
        except Exception:
            return None
    return None

def _normalize_text(text: str) -> str:
    # чтобы кэш работал лучше: убираем лишние пробелы
    return " ".join((text or "").strip().split())

@lru_cache(maxsize=4096)
def classify_text(text: str) -> Dict[str, Any]:
    """
    Локальная (на сервере) проверка текста через Ollama.
    Кэшируем результат, потому что “очень много текстов”.
    """
    normalized = _normalize_text(text)
    if not normalized:
        return {"label": "LEGAL", "reason": "empty"}

    prompt = f"{SYSTEM}\nText: {normalized}\n"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    def _invoke_ollama() -> Dict[str, Any]:
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        obj = json.loads(raw)
        model_text = obj.get("response", "") or ""
        parsed = _to_json_or_none(model_text)
        if not parsed:
            raise RuntimeError(f"non-JSON model output: {model_text[:200]}")
        # нормализуем выход
        label = str(parsed.get("label", "UNKNOWN")).upper()
        reason = str(parsed.get("reason", "")).strip()
        dt = time.perf_counter() - t0
        return {"label": label, "reason": reason, "t": dt}

    result = call_external_sync(
        "ollama",
        _invoke_ollama,
        retries=OLLAMA_RETRIES,
        fallback={"label": "UNKNOWN", "reason": "Ollama error: degraded mode", "t": None},
    )
    if isinstance(result, dict):
        return result
    return {"label": "UNKNOWN", "reason": "Ollama error: degraded mode", "t": None}
