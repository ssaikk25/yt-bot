"""Обёртка над YouTube Data API v3: учёт квоты, повторы, ошибки.

Важно: счётчик квоты — ГЛОБАЛЬНЫЙ и персистентный (logs/api_quota.json), а не
per-client. Раньше `YouTubeClient` создавался заново на каждый attempt, из-за
чего `quota_used` обнулялся и дневной бюджет 9000 не соблюдался.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .logger import get_logger

log = get_logger("youtube")

# Приблизительная стоимость запросов в единицах квоты (см. документацию API).
SEARCH_COST = 100
SIMPLE_COST = 1

try:  # Дневная квота YouTube сбрасывается в полночь Pacific Time.
    from zoneinfo import ZoneInfo
    _QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - fallback без tzdata
    _QUOTA_TZ = None


class YouTubeQuotaError(RuntimeError):
    """Превышена квота YouTube Data API."""


class YouTubeAuthError(RuntimeError):
    """Неверный/заблокированный API-ключ (401/403, не квота)."""


class YouTubeRateLimitError(RuntimeError):
    """Не удалось выполнить запрос после повторов (rate limit / 5xx)."""


def _today_key() -> str:
    now = datetime.now(_QUOTA_TZ) if _QUOTA_TZ else datetime.utcnow()
    return now.strftime("%Y-%m-%d")


class QuotaMeter:
    """Дневной счётчик квоты Data API, переживающий рестарты и пересоздание клиента."""

    def __init__(self, path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._used = 0
        self._day = ""
        self._path = None
        if path:
            self.bind(path)

    def bind(self, path) -> None:
        self._path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._day = str(data.get("day", ""))
            self._used = int(data.get("used", 0) or 0)
        except Exception:
            pass
        self._roll()

    def _roll(self) -> None:
        today = _today_key()
        if self._day != today:
            self._day = today
            self._used = 0

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"day": self._day, "used": self._used}), encoding="utf-8")
        except Exception as e:
            log.warning("API_QUOTA_ACCOUNTING: не удалось сохранить счётчик: %s", e)

    @property
    def used(self) -> int:
        with self._lock:
            self._roll()
            return self._used

    def add(self, cost: int) -> int:
        with self._lock:
            self._roll()
            self._used += int(cost)
            self._save()
            return self._used


# Глобальный счётчик: создаётся один раз на процесс.
quota_meter = QuotaMeter(Path(os.getenv("API_QUOTA_PATH", "logs/api_quota.json")))


def configure_quota_meter(path) -> None:
    quota_meter.bind(path)


def iso8601_duration_to_seconds(duration: str) -> float:
    """Парсит ISO 8601 duration (например PT1H2M3S, P1DT2H) в секунды."""
    total = 0.0
    if not duration:
        return total
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$", duration)
    if not m:
        return 0.0
    days, hours, minutes, seconds = m.groups()
    if days:
        total += int(days) * 86400
    if hours:
        total += int(hours) * 3600
    if minutes:
        total += int(minutes) * 60
    if seconds:
        total += float(seconds)
    return total


def pick_thumbnail(thumbnails: dict) -> dict:
    """Выбирает подходящую миниатюру с известными размерами (для оценки ориентации)."""
    for key in ("high", "medium", "default", "standard", "maxres"):
        t = (thumbnails or {}).get(key)
        if t and t.get("width") and t.get("height"):
            return t
    return {}


class YouTubeClient:
    def __init__(self, api_key: str, quota_budget: int = 9000):
        self.api_key = api_key
        self.quota_budget = quota_budget
        self._service = None

    @property
    def quota_used(self) -> int:
        return quota_meter.used

    @property
    def service(self):
        if self._service is None:
            if not self.api_key:
                raise RuntimeError("YOUTUBE_API_KEY не задан")
            http = httplib2.Http(timeout=30)  # против зависания Data API
            self._service = build("youtube", "v3", developerKey=self.api_key, http=http)
        return self._service

    def _charge(self, cost: int) -> None:
        used = quota_meter.add(cost)
        log.info("API_QUOTA_ACCOUNTING +%d -> %d/%d (день %s)",
                 int(cost), used, self.quota_budget, _today_key())

    def _execute(self, request, cost: int = SIMPLE_COST, retries: int = 4):
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                result = request.execute()
                self._charge(cost)
                return result
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", 0)
                content = getattr(e, "content", b"") or b""
                if isinstance(content, bytes):
                    content = content.decode("utf-8", "replace")
                lowered = content.lower()
                # Дневная квота (403 или 429 с "quota") — НЕ ретраим, ждём сброса.
                if status in (403, 429) and ("quota" in lowered or "quotaexceeded" in lowered):
                    raise YouTubeQuotaError(f"Quota превышена: {content[:300]}") from e
                # 401/403 без квоты: неверный ключ/доступ — НЕ ретраим.
                if status == 401 or (status == 403 and (
                        "key" in lowered or "forbidden" in lowered or "unauthorized" in lowered)):
                    raise YouTubeAuthError(f"Data API auth {status}: {content[:300]}") from e
                if status in (403, 429, 500, 502, 503, 504):
                    last_exc = e
                    wait = (2 ** attempt) * 1.5
                    log.warning(
                        "YouTube transient HTTP %s, повтор %d через %.1fs: %s",
                        status, attempt + 1, wait, content[:200],
                    )
                    time.sleep(wait)
                    continue
                raise
            except Exception as e:  # сетевые/парсинговые ошибки
                last_exc = e
                wait = 2 ** attempt
                log.warning("YouTube ошибка (%s), повтор %d через %.0fs", type(e).__name__, attempt + 1, wait)
                time.sleep(wait)
        raise YouTubeRateLimitError(f"Не удалось выполнить запрос после {retries} попыток: {last_exc}")

    def search_videos(
        self,
        query: str,
        region: Optional[str] = None,
        language: Optional[str] = None,
        max_results: int = 30,
        pages: int = 1,
        order: str = "relevance",
        safe_search: str = "none",
        video_duration: Optional[str] = None,
    ) -> list[dict]:
        items: list[dict] = []
        page_token: Optional[str] = None
        for _ in range(max(1, pages)):
            if self.quota_used >= self.quota_budget:
                raise YouTubeQuotaError("Достигнут бюджет квоты при поиске")
            kwargs = {
                "part": "snippet",
                "type": "video",
                "q": query,
                "maxResults": min(max(1, max_results), 50),
                "order": order,
                "safeSearch": safe_search,
            }
            if region:
                kwargs["regionCode"] = region
            if language:
                kwargs["relevanceLanguage"] = language
            if video_duration:
                kwargs["videoDuration"] = video_duration
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._execute(self.service.search().list(**kwargs), cost=SEARCH_COST)
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def get_video_details(self, video_ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        if not video_ids:
            return result
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            req = self.service.videos().list(
                part="snippet,contentDetails,statistics", id=",".join(chunk)
            )
            resp = self._execute(req, cost=SIMPLE_COST)
            for item in resp.get("items", []):
                result[item["id"]] = item
        return result

    def get_channels(self, channel_ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        if not channel_ids:
            return result
        for i in range(0, len(channel_ids), 50):
            chunk = channel_ids[i:i + 50]
            req = self.service.channels().list(
                part="snippet,statistics,contentDetails", id=",".join(chunk)
            )
            resp = self._execute(req, cost=SIMPLE_COST)
            for item in resp.get("items", []):
                result[item["id"]] = item
        return result

    def get_playlist_items(self, playlist_id: str, max_items: int = 50) -> list[dict]:
        items: list[dict] = []
        page_token: Optional[str] = None
        while len(items) < max_items:
            if self.quota_used >= self.quota_budget:
                log.warning("Бюджет квоты достигнут при получении загрузок канала")
                break
            kwargs = {
                "part": "contentDetails,snippet",
                "playlistId": playlist_id,
                "maxResults": min(50, max_items - len(items)),
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._execute(self.service.playlistItems().list(**kwargs), cost=SIMPLE_COST)
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items[:max_items]
