"""Обёртка над YouTube Data API v3: учёт квоты, повторы, ошибки."""
from __future__ import annotations

import re
import time
from typing import Optional

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .logger import get_logger

log = get_logger("youtube")

# Приблизительная стоимость запросов в единицах квоты (см. документацию API).
SEARCH_COST = 100
SIMPLE_COST = 1


class YouTubeQuotaError(RuntimeError):
    """Превышена квота YouTube Data API."""


class YouTubeRateLimitError(RuntimeError):
    """Не удалось выполнить запрос после повторов (rate limit / 5xx)."""


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
        self.quota_used = 0
        self._service = None

    @property
    def service(self):
        if self._service is None:
            if not self.api_key:
                raise RuntimeError("YOUTUBE_API_KEY не задан")
            http = httplib2.Http(timeout=30)  # против зависания Data API
            self._service = build("youtube", "v3", developerKey=self.api_key, http=http)
        return self._service

    def _charge(self, cost: int) -> None:
        self.quota_used += cost

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
