"""Точка входа: оркестрация поиска, оценки каналов и отправки в TagMe."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .access_control import configure_access_control
from .candidate_search import find_channels
from .channel_evaluator import ChannelEvaluator
from .config import load_settings
from .frame_sampler import FrameSampler
from .logger import get_logger, setup_logging
from .mimo_client import MiMoClient
from .tagme_bot import TagMeBot
from .video_selector import get_channel_video_pool
from .youtube_client import YouTubeClient, YouTubeQuotaError

log = get_logger("main")
history = get_logger("history")


def ensure_dirs(settings) -> None:
    Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.logs_dir).mkdir(parents=True, exist_ok=True)


def check_prereqs(settings) -> bool:
    ok = True
    if not settings.youtube_api_key:
        log.error("Не задан YOUTUBE_API_KEY (см. .env)")
        ok = False
    if not settings.mimo_api_key:
        log.error("Не задан MIMO_API_KEY (см. .env)")
        ok = False
    return ok


async def run() -> int:
    settings = load_settings()
    configure_access_control(
        settings.bot_block_cooldown, settings.bot_block_backoff_factor,
        settings.bot_block_max_cooldown, settings.video_403_cooldown,
    )
    ensure_dirs(settings)
    setup_logging(Path(settings.logs_dir))

    log.info("=== Запуск бота (DRY_RUN=%s) ===", settings.dry_run)
    if not check_prereqs(settings):
        return 2

    if not settings.queries:
        log.error("Список поисковых запросов пуст: проверьте config/search_queries.yaml")
        return 3

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

    # --- Поиск кандидатов ---
    try:
        channels = find_channels(youtube, settings)
    except YouTubeQuotaError as e:
        log.error("Quota YouTube: %s", e)
        return 4
    log.info("Найдено %d каналов-кандидатов (quota_used=%d)", len(channels), youtube.quota_used)

    if not channels:
        log.info("Кандидатов нет, завершение")
        return 0

    # --- TagMe ---
    bot = TagMeBot(settings)
    if not settings.dry_run:
        try:
            await bot.start()
        except Exception as e:
            log.error("Не удалось запустить TagMe: %s", e)
            await bot.close()
            return 5

    processed = 0
    sent = 0
    seen: set[str] = set()

    try:
        for channel in channels:
            if processed >= settings.max_channels_to_process:
                log.info("Достигнут лимит каналов (%d)", settings.max_channels_to_process)
                break
            if channel.channel_id in seen:
                continue
            seen.add(channel.channel_id)
            processed += 1

            try:
                status = await process_channel(settings, youtube, evaluator, bot, channel)
                if status == "sent":
                    sent += 1
            except Exception as e:
                log.exception("Ошибка обработки канала %s: %s", channel.title, e)
                history.info("CHANNEL ERROR | %s | %s | %s", channel.channel_id, channel.title, e)
                # Продолжаем со следующим кандидатом.
    finally:
        await bot.close()

    log.info("=== Готово: обработано %d, отправлено %d (quota_used=%d) ===", processed, sent, youtube.quota_used)
    return 0


async def process_channel(settings, youtube, evaluator, bot: TagMeBot, channel) -> str:
    log.info(
        "--- Канал: %s | подписчики=%d | видео=%d | %s ---",
        channel.title, channel.subscriber_count, channel.video_count, channel.channel_url,
    )

    pool, short_ratio = get_channel_video_pool(youtube, channel, settings)

    if short_ratio > settings.max_shorts_ratio:
        log.info(
            "Канал %s отброшен: доля Shorts/live %.0f%% > %.0f%%",
            channel.title, short_ratio * 100, settings.max_shorts_ratio * 100,
        )
        history.info("CHANNEL REJECT shorts/live | %s | %s | ratio=%.2f", channel.channel_id, channel.title, short_ratio)
        return "rejected"

    if not pool:
        log.info("Канал %s отброшен: пул пуст", channel.title)
        history.info("CHANNEL REJECT empty pool | %s | %s", channel.channel_id, channel.title)
        return "rejected"

    evaluation = evaluator.evaluate(channel, pool)

    if not evaluation.passed:
        log.info("Канал %s НЕ прошёл: %s", channel.title, evaluation.reason)
        history.info(
            "CHANNEL FAIL | %s | %s | %s | suitable=%d/%d",
            channel.channel_id, channel.title, evaluation.reason,
            evaluation.suitable, evaluation.eligible_count,
        )
        return "rejected"

    log.info(
        "Канал %s ПРОШЁЛ: категория %s (клавиша %s)",
        channel.title, evaluation.best_category, evaluation.best_category_key,
    )
    history.info(
        "CHANNEL PASS | %s | %s | %s | category=%s key=%s | suitable=%d/%d",
        channel.channel_id, channel.channel_url, channel.title,
        evaluation.best_category, evaluation.best_category_key,
        evaluation.suitable, evaluation.eligible_count,
    )

    if settings.dry_run:
        log.info(
            "DRY_RUN: отправил бы %s в категорию %s (клавиша %s)",
            channel.channel_url, evaluation.best_category, evaluation.best_category_key,
        )
        history.info(
            "DRY_RUN WOULD SEND | %s | %s | category=%s key=%s",
            channel.channel_id, channel.channel_url,
            evaluation.best_category, evaluation.best_category_key,
        )
        return "dry_run"

    try:
        status = await bot.submit_channel(
            channel.channel_url, evaluation.best_category_key, evaluation.best_category
        )
    except Exception as e:
        log.error("TagMe submit ошибка для %s: %s", channel.channel_url, e)
        history.info("TAGME ERROR | %s | %s | %s", channel.channel_id, channel.channel_url, e)
        return "error"

    if status == "exists":
        log.info("Ссылка %s уже есть в базе — пропускаю", channel.channel_url)
        history.info("SKIP exists | %s | %s", channel.channel_id, channel.channel_url)
        return "exists"

    if status == "sent":
        log.info("Отправлено: %s (%s)", channel.channel_url, evaluation.best_category)
        history.info(
            "SENT | %s | %s | category=%s key=%s",
            channel.channel_id, channel.channel_url,
            evaluation.best_category, evaluation.best_category_key,
        )
        return "sent"

    log.warning("Канал %s: отправка не подтверждена, перехожу к следующему", channel.title)
    history.info("SEND UNCONFIRMED | %s | %s", channel.channel_id, channel.channel_url)
    return "unconfirmed"


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Прервано пользователем")
        return 130


if __name__ == "__main__":
    sys.exit(main())
