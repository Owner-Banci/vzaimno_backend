# app/moderation_image.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import get_env


NSFW_MODEL_ID = get_env("NSFW_MODEL_ID", "hf_hub:Marqo/nsfw-image-detection-384") or "hf_hub:Marqo/nsfw-image-detection-384"
NSFW_DEVICE = get_env("NSFW_DEVICE", "cpu") or "cpu"  # cpu / mps (если есть)
NSFW_CACHE_DIR = get_env("NSFW_CACHE_DIR", "") or ""

_detector = None  # singleton

@dataclass
class NsfwResult:
    nsfw: float
    sfw: float
    top_label: str
    top_prob: float
    infer_seconds: float

def _pick_device(requested: str) -> str:
    d = (requested or "cpu").lower()
    if d == "mps":
        try:
            import torch
            if torch.backends.mps.is_available() and torch.backends.mps.is_built():
                return "mps"
        except Exception:
            pass
    return "cpu"


def _cache_root() -> Path:
    raw = NSFW_CACHE_DIR or get_env("MODEL_CACHE_DIR", "") or ""
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    else:
        candidates.append(Path("/tmp/vzaimno_model_cache"))
        uploads_dir = get_env("UPLOADS_DIR", "uploads") or "uploads"
        candidates.append(Path(uploads_dir).expanduser() / "model_cache")

    for candidate in candidates:
        root = candidate
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return root
        except OSError:
            continue

    raise RuntimeError("No writable cache directory for NSFW model")


def _prepare_model_cache_env() -> Path:
    root = _cache_root()
    hf_home = root / "huggingface"
    torch_home = root / "torch"
    xdg_cache = root / "xdg"
    for path in (hf_home, torch_home, xdg_cache):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(hf_home / "hub"))
    os.environ.setdefault("TORCH_HOME", str(torch_home))
    os.environ.setdefault("XDG_CACHE_HOME", str(xdg_cache))
    return hf_home / "hub"


class NsfwTimmDetector:
    def __init__(self, model_id: str, device: str = "cpu"):
        cache_dir = _prepare_model_cache_env()

        import timm
        import torch

        self.model_id = model_id
        self.device = _pick_device(device)

        t0 = time.perf_counter()
        self.model = timm.create_model(self.model_id, pretrained=True, cache_dir=str(cache_dir))
        self.model.eval()
        if self.device != "cpu":
            self.model.to(self.device)

        data_config = timm.data.resolve_model_data_config(self.model)
        self.transforms = timm.data.create_transform(**data_config, is_training=False)

        cfg = getattr(self.model, "pretrained_cfg", {}) or {}
        self.class_names = cfg.get("label_names", ["NSFW", "SFW"])  # часто ["NSFW","SFW"]
        self.load_seconds = time.perf_counter() - t0

        # индекс класса NSFW
        self.nsfw_index = 0
        for i, name in enumerate(self.class_names):
            if str(name).upper() == "NSFW":
                self.nsfw_index = i
                break

    def predict_bytes(self, image_bytes: bytes) -> NsfwResult:
        import torch
        from PIL import Image

        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        x = self.transforms(img).unsqueeze(0)

        if self.device != "cpu":
            x = x.to(self.device)

        t0 = time.perf_counter()
        with torch.no_grad():
            out = self.model(x).softmax(dim=-1).detach().cpu()[0]
        infer_dt = time.perf_counter() - t0

        probs = [float(v) for v in out.tolist()]
        # безопасно: если внезапно 1 класс — считаем его nsfw
        nsfw = probs[self.nsfw_index] if self.nsfw_index < len(probs) else float(probs[0])
        sfw = 1.0 - nsfw if len(probs) >= 2 else float(1.0 - nsfw)

        # top
        top_i = max(range(len(probs)), key=lambda i: probs[i])
        top_label = str(self.class_names[top_i]) if top_i < len(self.class_names) else str(top_i)
        top_prob = float(probs[top_i])

        return NsfwResult(
            nsfw=float(nsfw),
            sfw=float(sfw),
            top_label=top_label,
            top_prob=top_prob,
            infer_seconds=float(infer_dt),
        )

def get_nsfw_detector() -> NsfwTimmDetector:
    global _detector
    if _detector is None:
        _detector = NsfwTimmDetector(model_id=NSFW_MODEL_ID, device=NSFW_DEVICE)
    return _detector
