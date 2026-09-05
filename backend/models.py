from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


class Rect(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def stays_inside_frame(self):
        if self.x + self.w > 1 or self.y + self.h > 1:
            raise ValueError("Khung phải nằm hoàn toàn bên trong video.")
        return self


class AnalyzeRequest(BaseModel):
    canonicalUrl: str
    cookieText: str = Field(min_length=1, max_length=2_000_000)
    geminiApiKey: str = Field(min_length=10, max_length=512)
    blurMode: Literal["auto", "manual"] = "auto"
    voiceCount: int = Field(default=1, ge=1, le=4)
    mediaUrl: str | None = Field(default=None, max_length=16384)
    browserUserAgent: str = Field(default="", max_length=1024, pattern=r"^[^\r\n]*$")


class RenderRequest(BaseModel):
    voiceMap: dict[str, str]
    blurRegions: list[Rect] = Field(default_factory=list)
    subtitleRect: Rect
    speechRate: float = Field(default=1.0, ge=.8, le=1.4)
    previewOnly: bool = False
    timingMode: Literal["extend_video", "fit_audio"] = "extend_video"

    @model_validator(mode="after")
    def reasonable_size(self):
        if not self.voiceMap or len(self.voiceMap) > 50:
            raise ValueError("Danh sách giọng không hợp lệ.")
        if len(self.blurRegions) > 20:
            raise ValueError("Chỉ hỗ trợ tối đa 20 vùng blur.")
        if any(len(key) > 40 or len(value) > 100 for key, value in self.voiceMap.items()):
            raise ValueError("Tên người nói hoặc giọng quá dài.")
        return self
