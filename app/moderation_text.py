# app/moderation_text.py
from __future__ import annotations

import json
import os
import time
import urllib.request
from functools import lru_cache
from typing import Any, Dict, Optional

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "shieldgemma:2b")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "15"))

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

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        obj = json.loads(raw)
        model_text = obj.get("response", "") or ""
        parsed = _to_json_or_none(model_text)
        if not parsed:
            return {"label": "UNKNOWN", "reason": f"Model ответила не-JSON: {model_text[:200]}"}
        # нормализуем выход
        label = str(parsed.get("label", "UNKNOWN")).upper()
        reason = str(parsed.get("reason", "")).strip()
        dt = time.perf_counter() - t0
        return {"label": label, "reason": reason, "t": dt}
    except Exception as e:
        return {"label": "UNKNOWN", "reason": f"Ollama error: {e!s}"}