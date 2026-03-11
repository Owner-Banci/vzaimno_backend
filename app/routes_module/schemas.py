from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class RouteTaskByPathOut(BaseModel):
    id: str
    title: str
    category: Optional[str] = None
    address_text: Optional[str] = None
    distance_to_route_meters: float = Field(default=0)
    price_text: Optional[str] = None
    preview_image_url: Optional[str] = None
    status: Optional[str] = None


class RouteDetailsOut(BaseModel):
    entity_id: str
    start_address: str
    end_address: str
    distance_meters: int = 0
    duration_seconds: int = 0
    distance_text: str
    duration_text: str
    polyline: List[List[float]] = Field(default_factory=list)
    tasks_by_route: List[RouteTaskByPathOut] = Field(default_factory=list)
