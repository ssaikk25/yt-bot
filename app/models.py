"""Датаклассы-модели предметной области."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class VideoCandidate:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    published_at: str = ""
    description: str = ""
    thumbnail_url: str = ""
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    duration_seconds: float = 0.0
    definition: str = ""  # "hd" | "sd"
    is_live: bool = False
    is_short: bool = False
    is_vertical: bool = False
    thumb_w: int = 0
    thumb_h: int = 0

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class ChannelCandidate:
    channel_id: str
    title: str
    custom_url: str = ""      # может быть @handle или /c/... или пусто
    handle: str = ""          # только часть после @ (без "@")
    subscriber_count: int = 0
    video_count: int = 0
    view_count: int = 0        # суммарные просмотры канала (для средних на видео)
    uploads_playlist_id: str = ""
    country: str = ""
    popularity_score: float = 0.0  # меньше = более непопулярный (приоритетнее)

    @property
    def channel_url(self) -> str:
        if self.handle:
            return f"https://www.youtube.com/@{self.handle}"
        if self.custom_url.startswith("http"):
            return self.custom_url
        return f"https://www.youtube.com/channel/{self.channel_id}"


@dataclass
class VideoMeta:
    video_id: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    media_url: str = ""
    ext: str = "mp4"
    media_url_created_at: float = 0.0   # unixtime получения media_url
    media_url_expire: float = 0.0       # unixtime истечения (из expire=), 0 = неизвестно

    @property
    def media_url_expired(self) -> bool:
        """True, если у media_url известен expire и он уже в прошлом."""
        return self.media_url_expire > 0 and time.time() > self.media_url_expire


@dataclass
class TechnicalResult:
    ok: bool
    width: int = 0
    height: int = 0
    duration: float = 0.0
    is_vertical: bool = False
    resolution_ok: bool = False
    is_static: bool = False
    motion_score: float = 1.0
    error: str = ""


@dataclass
class FrameAnalysis:
    suitable: bool
    category: str
    description: str
    confidence: float
    reason: str
    flags: dict[str, bool] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalVlmResult:
    """Результат локального VLM pre-filter (vision, через llama.cpp).

    verdict: "OBVIOUS_REJECT" | "UNCERTAIN" | "NO_OBVIOUS_REJECT"
    """
    verdict: str = ""
    flags: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    reason: str = ""
    error: str = ""                # непусто, если inference завершился ошибкой
    latency_sec: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True, если локальная модель реально дала ответ (не ошибка)."""
        return not self.error and self.verdict in ("OBVIOUS_REJECT", "UNCERTAIN", "NO_OBVIOUS_REJECT")

    @property
    def is_reject(self) -> bool:
        return self.ok and self.verdict == "OBVIOUS_REJECT"

    def auto_reject_triggered(self, allowed_flags: set[str]) -> bool:
        """Активный режим: reject только по allowlist-признакам."""
        if not allowed_flags or not self.is_reject:
            return False
        return any(f in allowed_flags for f in self.flags)


@dataclass
class VideoEvaluation:
    video: VideoCandidate
    technical: Optional[TechnicalResult] = None
    analysis: Optional[FrameAnalysis] = None
    suitable: bool = False
    reject_reason: str = ""
    temp_error: bool = False      # временная ошибка (bot-check/403/expire/network)
    error_kind: str = ""          # код ошибки из access_control
    local_vlm: Optional[LocalVlmResult] = None


@dataclass
class ChannelEvaluation:
    channel: ChannelCandidate
    eligible_count: int = 0
    checked: int = 0
    suitable: int = 0
    passed: bool = False
    best_category: str = ""
    best_category_key: str = ""
    reason: str = ""
    evaluations: list[VideoEvaluation] = field(default_factory=list)
    short_live_ratio: float = 0.0
    temp_error: bool = False      # хотя бы одно видео дало temp-ошибку (не решено)
    farm_risk: float = 0.0        # channel-level AI-farm risk (дешёвый, без MiMo)
    excluded_count: int = 0       # видео, отсеянных дешёвыми фильтрами до MiMo
    unavailable_count: int = 0    # видео, признанных недоступными на probe (вне denominator)
