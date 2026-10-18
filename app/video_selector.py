"""Отбор видео конкретного канала в пул для проверки (по последним загрузкам)."""
from __future__ import annotations

from .logger import get_logger
from .models import ChannelCandidate, VideoCandidate
from .precheck import is_hard_junk_title
from .youtube_client import YouTubeClient, iso8601_duration_to_seconds, pick_thumbnail

log = get_logger("selector")

SHORT_MARKERS = ("#short", "#shorts", "shorts", "short:")


def _access_ok(content: dict, target_region: str) -> tuple[bool, str]:
    """Дешёвая проверка доступности по API metadata (до yt-dlp probe).

    Возвращает (ok, reason). Не считает само наличие regionRestriction отказом:
    смотрим только, доступно ли видео в целевом регионе (если он задан).
    """
    # age-gate: yt-dlp без cookies не скачает
    rating = (content.get("contentRating") or {}).get("ytRating", "")
    if rating == "ytAgeRestricted":
        return False, "age_restricted"

    if target_region:
        rr = content.get("regionRestriction") or {}
        blocked = rr.get("blocked") or []
        allowed = rr.get("allowed") or []
        if target_region in [b.upper() for b in blocked]:
            return False, "region_blocked"
        if allowed and target_region not in [a.upper() for a in allowed]:
            return False, "region_not_allowed"
    return True, ""


def get_channel_video_pool(youtube: YouTubeClient, channel: ChannelCandidate, settings) -> tuple[list[VideoCandidate], float]:
    """Возвращает (пул видео, прошедших дешёвые фильтры; долю Shorts/live в выборке).

    Пул содержит только видео, которые НЕ являются Shorts/live/вертикальными по дешёвым
    признакам из API. Точная проверка разрешения/ориентации выполняется позже через ffmpeg.
    """
    if not channel.uploads_playlist_id:
        log.warning("Нет uploads playlist для канала %s", channel.channel_id)
        return [], 0.0

    items = youtube.get_playlist_items(channel.uploads_playlist_id, max_items=settings.max_videos_per_channel)
    total = len(items)

    candidates: list[VideoCandidate] = []
    seen: set[str] = set()
    for it in items:
        vid = (it.get("contentDetails", {}) or {}).get("videoId")
        snippet = it.get("snippet", {})
        if not vid or vid in seen:
            continue
        seen.add(vid)
        candidates.append(VideoCandidate(
            video_id=vid,
            channel_id=channel.channel_id,
            channel_title=channel.title,
            title=snippet.get("title", ""),
            published_at=snippet.get("publishedAt", ""),
        ))

    details = youtube.get_video_details([c.video_id for c in candidates])
    pool: list[VideoCandidate] = []
    short_live = 0
    junk = 0
    for v in candidates:
        d = details.get(v.video_id)
        if not d:
            continue  # недоступно/удалено
        snippet = d.get("snippet", {})
        content = d.get("contentDetails", {})
        stats = d.get("statistics", {})
        title = snippet.get("title", "") or v.title
        live = snippet.get("liveBroadcastContent", "none") in ("live", "upcoming")
        duration = iso8601_duration_to_seconds(content.get("duration", ""))
        definition = content.get("definition", "")

        title_lower = title.lower()
        has_marker = any(m in title_lower for m in SHORT_MARKERS)
        thumb = pick_thumbnail(snippet.get("thumbnails", {}))
        tw, th = thumb.get("width", 0), thumb.get("height", 0)
        is_vertical = th > 0 and tw > 0 and th > tw
        is_short = has_marker or (is_vertical and duration <= settings.shorts_max_duration)

        v.title = title
        v.description = snippet.get("description", "") or ""
        v.thumbnail_url = thumb.get("url", "") or ""
        v.view_count = int(stats.get("viewCount", 0) or 0)
        v.like_count = int(stats.get("likeCount", 0) or 0)
        v.duration_seconds = duration
        v.definition = definition
        v.is_live = live
        v.is_short = is_short
        v.is_vertical = is_vertical
        v.thumb_w, v.thumb_h = tw, th

        if live or is_short:
            short_live += 1
            continue
        if is_vertical:
            continue  # вертикальные видео не являются eligible
        if settings.max_views > 0 and v.view_count >= settings.max_views:
            continue  # только малопросматриваемые видео
        if settings.video_definition_filter and definition == "sd":
            continue  # API уже говорит <720p — не тратим probe/ffmpeg
        if duration <= settings.min_duration_seconds:
            continue  # короче минимума — не тратим probe/ffmpeg
        ok_access, access_reason = _access_ok(content, settings.target_region)
        if not ok_access:
            log.info("Канал %s: видео %s отсеяно до probe (%s)", channel.title, vid, access_reason)
            continue
        if is_hard_junk_title(title):
            junk += 1
            continue  # очевидный мусор (gameplay/white-noise/...) — не тратим MiMo
        pool.append(v)

    ratio = (short_live / total) if total else 0.0
    if junk:
        log.info("Канал %s: отсеяно очевидного мусора по title: %d", channel.title, junk)
    # Внутри пула сначала самые непопулярные видео (низкие просмотры).
    pool.sort(key=lambda v: v.view_count)
    return pool, ratio
