# app/moderation_image.py
from __future__ import annotations

import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

from app.config import get_env


NSFW_MODEL_ID = get_env("NSFW_MODEL_ID", "hf_hub:Marqo/nsfw-image-detection-384") or "hf_hub:Marqo/nsfw-image-detection-384"
NSFW_DEVICE = get_env("NSFW_DEVICE", "cpu") or "cpu"  # cpu / mps (если есть)

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

class NsfwTimmDetector:
    def __init__(self, model_id: str, device: str = "cpu"):
        import timm
        import torch

        self.model_id = model_id
        self.device = _pick_device(device)

        t0 = time.perf_counter()
        self.model = timm.create_model(self.model_id, pretrained=True)
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
