"""Поиск N подходящих каналов (без отправки в TagMe) и вывод их ссылок.

Использует тот же пайплайн, что и main.py (поиск -> дешёвые фильтры -> технические
проверки -> MiMo -> правило 30%), но не трогает TagMe. Результат пишется в
logs/found_channels.json и выводится в консоль.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .access_control import configure_access_control
from .candidate_search import find_channels
from .category_mapper import canonicalize
from .channel_evaluator import ChannelEvaluator
from .config import load_settings
from .frame_sampler import FrameSampler
from .logger import get_logger, setup_logging
from .mimo_client import MiMoClient
from .video_selector import get_channel_video_pool
from .youtube_client import YouTubeClient, YouTubeQuotaError

log = get_logger("finder")


def _summarize(channel, ev) -> dict:
    checked_videos = []
    for vv in ev.evaluations:
        cat = ""
        if vv.analysis:
            cat = canonicalize(vv.analysis.category) or vv.analysis.category
        checked_videos.append({
            "id": vv.video.video_id,
            "title": vv.video.title[:100],
            "views": vv.video.view_count,
            "suitable": vv.suitable,
            "category": cat,
            "reason": vv.reject_reason or "",
        })
    return {
        "url": channel.channel_url,
        "title": channel.title,
        "channel_id": channel.channel_id,
        "subscribers": channel.subscriber_count,
        "video_count": channel.video_count,
        "category": ev.best_category,
        "category_key": ev.best_category_key,
        "eligible": ev.eligible_count,
        "checked": ev.checked,
        "suitable": ev.suitable,
        "checked_videos": checked_videos,
    }


def run(target: int) -> list[dict]:
    settings = load_settings()
    configure_access_control(
        settings.bot_block_cooldown, settings.bot_block_backoff_factor,
        settings.bot_block_max_cooldown, settings.video_403_cooldown,
    )
    Path(settings.logs_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
    setup_logging(Path(settings.logs_dir))

    log.info("=== FINDER: ищу %d каналов (MAX_VIEWS=%d) ===", target, settings.max_views)

    youtube = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
    mimo = MiMoClient(
        api_key=settings.mimo_api_key,
        base_url=settings.mimo_base_url,
        model=settings.mimo_model,
        categories=settings.categories,
        timeout=settings.mimo_timeout,
        retries=settings.mimo_retries,
        max_tokens=settings.mimo_max_tokens,
        temperature=settings.mimo_temperature,
        use_response_format=settings.mimo_use_response_format,
        enable_thinking=settings.mimo_enable_thinking,
    )
    sampler = FrameSampler(
        settings.ffmpeg_path, settings.ffprobe_path, Path(settings.temp_dir),
        delay=settings.ytdlp_delay, cookies_file=settings.cookies_file,
        format_mode=settings.video_format_mode,
        frame_extraction_mode=settings.frame_extraction_mode,
        video_error_cooldown_sec=settings.video_403_cooldown,
    )
    evaluator = ChannelEvaluator(settings, youtube, mimo, sampler, parallel=settings.eval_parallel)

    try:
        channels = find_channels(youtube, settings)
    except YouTubeQuotaError as e:
        log.error("Quota YouTube: %s", e)
        return []

    log.info("Каналов-кандидатов после дешёвых фильтров: %d", len(channels))

    results: list[dict] = []
    seen: set[str] = set()
    skipped = {"few": 0, "shorts": 0, "fail": 0, "error": 0}

    for idx, ch in enumerate(channels, 1):
        if len(results) >= target:
            break
        if idx > settings.max_channels_to_process:
            log.info("Достигнут лимит обработки каналов (%d)", settings.max_channels_to_process)
            break
        if ch.channel_id in seen:
            continue
        seen.add(ch.channel_id)

        try:
            pool, ratio = get_channel_video_pool(youtube, ch, settings)
            if ratio > settings.max_shorts_ratio:
                skipped["shorts"] += 1
                continue
            if not pool:
                skipped["few"] += 1
                continue

            ev = evaluator.evaluate(ch, pool)
            if ev.passed:
                results.append(_summarize(ch, ev))
                log.info(
                    ">>> НАЙДЕН #%d: %s | категория=%s | подходящих=%d/%d",
                    len(results), ch.channel_url, ev.best_category, ev.suitable, ev.eligible_count,
                )
            else:
                skipped["fail"] += 1
        except Exception as e:
            skipped["error"] += 1
            log.warning("Ошибка канала %s: %s", ch.title, e)

        if idx % 10 == 0:
            log.info(
                "Прогресс: обработано %d, найдено %d (few=%d shorts=%d fail=%d err=%d)",
                idx, len(results), skipped["few"], skipped["shorts"], skipped["fail"], skipped["error"],
            )

    log.info("Итог: найдено %d каналов (квота=%d)", len(results), youtube.quota_used)

    out_path = Path(settings.logs_dir) / "found_channels.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Результат сохранён в %s", out_path)
    return results


def _print(results: list[dict]) -> None:
    max_views = int(os.getenv("MAX_VIEWS", "0")) or 0
    print("\n" + "=" * 72)
    print(f"НАЙДЕННЫЕ КАНАЛЫ (видео < {max_views} просмотров):")
    print("=" * 72)
    for i, r in enumerate(results, 1):
        print(f"{i:2d}. {r['url']}")
        print(f"    Название: {r['title']} | подписчиков: {r['subscribers']} | видео: {r['video_count']}")
        print(f"    Категория канала: {r['category']} | подходят {r['suitable']}/{r['eligible']} (проверено {r['checked']})")
        for v in r["checked_videos"]:
            mark = "+" if v["suitable"] else "-"
            detail = v["category"] if v["suitable"] else (v["reason"] or "не подходит")
            print(f"      [{mark}] {v['title'][:55]} | {v['views']} просмотров | {detail}")
    print("=" * 72)


def main() -> int:
    target = int(os.getenv("TARGET_CHANNELS", "10") or 10)
    try:
        results = run(target)
    except KeyboardInterrupt:
        log.info("Прервано пользователем")
        return 130
    _print(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
