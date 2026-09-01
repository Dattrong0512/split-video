from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Rect(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class AnalyzeRequest(BaseModel):
    canonicalUrl: str
    cookieText: str
    geminiApiKey: str
    blurMode: Literal["auto", "manual"] = "auto"


class RenderRequest(BaseModel):
    voiceMap: dict[str, str]
    blurRegions: list[Rect] = Field(default_factory=list)
    subtitleRect: Rect
