"""Поиск кандидатов: видео -> каналы, дешёвые фильтры, приоритизация по непопулярности.

Порядок работы (экономно по квоте и без MiMo):
1. Редкие/узкие запросы через search.list.
2. Обогащение видео через videos.list (просмотры, длительность, live/shorts, ориентация).
3. Группировка по channel_id, получение каналов через channels.list.
4. Отсев каналов с малым числом видео и сортировка по низкой популярности.
"""
from __future__ import annotations

from .logger import get_logger
from .models import ChannelCandidate, VideoCandidate
from .youtube_client import YouTubeClient, YouTubeQuotaError, iso8601_duration_to_seconds, pick_thumbnail

log = get_logger("search")

SHORT_MARKERS = ("#short", "#shorts", "shorts", "short:")


def _video_from_search_item(item: dict) -> VideoCandidate | None:
    item_id = item.get("id")
    if isinstance(item_id, dict):
        video_id = item_id.get("videoId")
    else:
        video_id = item_id
    snippet = item.get("snippet", {})
    if not video_id or not snippet.get("channelId"):
        return None
    return VideoCandidate(
        video_id=video_id,
        channel_id=snippet["channelId"],
        channel_title=snippet.get("channelTitle", ""),
        title=snippet.get("title", ""),
        published_at=snippet.get("publishedAt", ""),
    )


def collect_video_candidates(youtube: YouTubeClient, settings, queries=None) -> list[VideoCandidate]:
    seen: set[str] = set()
    out: list[VideoCandidate] = []
    for q in (queries if queries is not None else settings.queries):
        if youtube.quota_used >= settings.youtube_quota_budget:
            log.warning("Бюджет квоты достигнут, остановка поиска")
            break
        query = q.get("query", "").strip()
        if not query:
            continue
        region = q.get("region") or None
        language = q.get("language") or None
        order = q.get("order", "relevance")
        video_duration = q.get("video_duration") or None
        max_results = int(q.get("max_results", settings.default_search_max_results))
        pages = int(q.get("pages", settings.max_search_pages_per_query))
        try:
            items = youtube.search_videos(query, region, language, max_results, pages, order, video_duration=video_duration)
        except YouTubeQuotaError:
            raise  # не глотаем quota — пусть вызывающий код ждёт сброса
        except Exception as e:
            log.warning("Ошибка поиска по запросу %r: %s", query, e)
            continue
        for item in items:
            v = _video_from_search_item(item)
            if v and v.video_id not in seen:
                seen.add(v.video_id)
                out.append(v)
        log.info("Запрос %r (region=%s) -> %d видео, всего уникальных %d", query, region or "-", len(items), len(out))
    return out


def enrich_and_filter(youtube: YouTubeClient, candidates: list[VideoCandidate], settings) -> list[VideoCandidate]:
    """Дополняет видео статистикой и контентом, отбрасывает live/upcoming."""
    if not candidates:
        return []
    details = youtube.get_video_details([c.video_id for c in candidates])
    out: list[VideoCandidate] = []
    for c in candidates:
        d = details.get(c.video_id)
        if not d:
            continue  # недоступно/удалено/private
        snippet = d.get("snippet", {})
        content = d.get("contentDetails", {})
        stats = d.get("statistics", {})
        title = snippet.get("title", "") or c.title
        live = snippet.get("liveBroadcastContent", "none") in ("live", "upcoming")
        duration = iso8601_duration_to_seconds(content.get("duration", ""))

        title_lower = title.lower()
        has_marker = any(m in title_lower for m in SHORT_MARKERS)
        thumb = pick_thumbnail(snippet.get("thumbnails", {}))
        tw, th = thumb.get("width", 0), thumb.get("height", 0)
        is_vertical = th > 0 and tw > 0 and th > tw
        is_short = has_marker or (is_vertical and duration <= settings.shorts_max_duration)

        c.title = title
        c.published_at = snippet.get("publishedAt", "") or c.published_at
        c.view_count = int(stats.get("viewCount", 0) or 0)
        c.like_count = int(stats.get("likeCount", 0) or 0)
        c.comment_count = int(stats.get("commentCount", 0) or 0)
        c.duration_seconds = duration
        c.definition = content.get("definition", "")
        c.is_live = live
        c.is_short = is_short
        c.is_vertical = is_vertical
        c.thumb_w, c.thumb_h = tw, th

        if live:
            continue
        # Просмотры — сигнал приоритета, а не жёсткий отказ: один вирусный ролик
        # не должен отбраковывать весь канал. Итоговый фильтр <max_views остаётся
        # на уровне eligibility (video_selector).
        out.append(c)
    return out


def build_channel_candidates(youtube: YouTubeClient, videos: list[VideoCandidate], settings) -> list[ChannelCandidate]:
    grouped: dict[str, list[VideoCandidate]] = {}
    for v in videos:
        if v.is_short:
            continue
        grouped.setdefault(v.channel_id, []).append(v)

    if not grouped:
        return []

    channels_raw = youtube.get_channels(list(grouped.keys()))
    out: list[ChannelCandidate] = []
    for cid, vids in grouped.items():
        ch = channels_raw.get(cid)
        if not ch:
            continue
        snippet = ch.get("snippet", {})
        stats = ch.get("statistics", {})
        content = ch.get("contentDetails", {})
        video_count = int(stats.get("videoCount", 0) or 0)
        if video_count < settings.min_channel_videos:
            log.info(
                "Канал %s (%s) отброшен: видео %d < %d",
                cid, snippet.get("title", ""), video_count, settings.min_channel_videos,
            )
            continue

        custom_url = snippet.get("customUrl", "") or ""
        handle = ""
        if "@" in custom_url:
            handle = custom_url.split("@", 1)[1].split("/")[0].strip()

        subs = int(stats.get("subscriberCount", 0) or 0)
        avg_views = sum(v.view_count for v in vids) / max(1, len(vids))
        popularity_score = subs + avg_views

        cc = ChannelCandidate(
            channel_id=cid,
            title=snippet.get("title", ""),
            custom_url=custom_url,
            handle=handle,
            subscriber_count=subs,
            video_count=video_count,
            uploads_playlist_id=(content.get("relatedPlaylists", {}) or {}).get("uploads", ""),
            country=snippet.get("country", "") or "",
            popularity_score=popularity_score,
        )
        out.append(cc)

    # Самые непопулярные первыми.
    out.sort(key=lambda c: c.popularity_score)
    return out


def find_channels(youtube: YouTubeClient, settings, queries=None) -> list[ChannelCandidate]:
    videos = collect_video_candidates(youtube, settings, queries)
    videos = enrich_and_filter(youtube, videos, settings)
    return build_channel_candidates(youtube, videos, settings)
