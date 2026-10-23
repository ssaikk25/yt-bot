"""Автономный ночной поиск каналов.

- Сам поднимает POT-сервер (PO-token) для обхода bot-check, если его нет.
- Ждёт сброса квоты YouTube (ретрай с паузой при quotaExceeded).
- Ищет каналы и оценивает их по правилу 30%, сохраняет результат инкрементально
  в logs/found_channels.json (переживает любые сбои).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .access_control import TEMP_KINDS, BotBlockedError, access_controller, access_metrics, classify_youtube_error, configure_access_control
from .candidate_search import find_channels
from .category_mapper import canonicalize
from .channel_evaluator import ChannelEvaluator
from .config import load_settings
from .discovery import discover_channels_ytdlp, recovery_probe
from .frame_sampler import FrameSampler, is_bot_blocked
from .deepseek_client import create_vision_client
from .logger import get_logger, setup_logging
from .video_selector import get_channel_video_pool
from .youtube_client import YouTubeClient, YouTubeQuotaError

log = get_logger("overnight")


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


def _save(results: list[dict], settings) -> None:
    out = Path(settings.logs_dir) / "found_channels.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    log.info("Сохранено %d каналов в %s", len(results), out)


def _checked_path(settings) -> Path:
    return Path(settings.logs_dir) / "checked_channels.json"


def _load_checked(settings) -> set[str]:
    """Уже проверенные каналы (и найденные, и отклонённые) — переживают рестарт."""
    p = _checked_path(settings)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x) for x in data}
    except Exception:
        pass
    return set()


def _save_checked(settings, seen: set[str]) -> None:
    p = _checked_path(settings)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _mark_seen(seen: set[str], settings, cid: str) -> None:
    """Добавляет канал в seen и сразу сохраняет на диск (переживает рестарт)."""
    if cid not in seen:
        seen.add(cid)
        _save_checked(settings, seen)


def _ensure_pot_server(settings) -> None:
    """Проверяет/запускает локальный POT-сервер (bgutil) на 127.0.0.1:4416."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            if s.connect_ex(("127.0.0.1", 4416)) == 0:
                log.info("POT-сервер уже работает")
                return
    except Exception:
        pass

    server_dir = settings.pot_server_dir or ""
    main_js = Path(server_dir) / "build" / "main.js" if server_dir else None
    if not (main_js and main_js.exists()):
        log.warning("POT-сервер не найден по POT_SERVER_DIR=%r — frame-извлечение может быть заблокировано", server_dir)
        return
    try:
        subprocess.Popen(
            ["node", "build/main.js", "--port", "4416"],
            cwd=str(Path(server_dir)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Запущен POT-сервер из %s", server_dir)
        time.sleep(6)
    except Exception as e:
        log.warning("Не удалось запустить POT-сервер: %s", e)


def _wait_for_unblock(settings, chunk: float = 300.0) -> None:
    """ЕДИНЫЙ координатор паузы: ждёт окончания cooldown кусками (логи каждые chunk c).

    Куски нужны, чтобы watchdog видел активность и не перезапускал процесс.
    """
    while access_controller.is_blocked():
        remaining = access_controller.cooldown_remaining()
        log.info("COOLDOWN_REMAINING %d c (бот ждёт, прямые запросы к youtube.com запрещены)",
                 int(remaining))
        time.sleep(min(chunk, remaining))
    if access_controller.state == access_controller.BLOCKED:
        log.info("COOLDOWN_EXPIRED")


def _api_query_batch(settings) -> list[dict]:
    """YAML-запросы + ротируемая выборка из пула — для Data API (в пределах квоты)."""
    import random
    from .expansion_pool import EXPANSION_POOL, EXPERIMENT_POOL
    from .expansion_pool_950 import EXPANSION_950
    from .query_pool import QUERY_POOL

    base = list(settings.queries)
    pool = ([dict(q) for q in QUERY_POOL] + [dict(q) for q in EXPANSION_POOL]
            + [dict(q) for q in EXPERIMENT_POOL] + [dict(q) for q in EXPANSION_950])
    pool = [q for q in pool
            if (q.get("category") or "").strip() != "Аниме/Анимации" and q.get("query")]
    sample = random.sample(pool, min(30, len(pool))) if pool else []
    return base + sample


def main() -> int:
    target = int(os.getenv("TARGET_CHANNELS", "50") or 50)
    max_hours = float(os.getenv("MAX_HOURS", "14") or 14)
    quota_wait = int(os.getenv("QUOTA_WAIT_SECONDS", "1800") or 1800)

    settings = load_settings()
    Path(settings.logs_dir).mkdir(parents=True, exist_ok=True)
    configure_access_control(
        settings.bot_block_cooldown,
        settings.bot_block_backoff_factor,
        settings.bot_block_max_cooldown,
        settings.video_403_cooldown,
        state_path=Path(settings.logs_dir) / "access_state.json",
    )
    Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
    setup_logging(Path(settings.logs_dir))
    access_controller.load_state()

    log.info("=== OVERNIGHT: target=%d, max_hours=%s, MAX_VIEWS=%d ===", target, max_hours, settings.max_views)
    log.info("ACCESS_STATE конфиг: base=%.0f backoff=%.1f max=%.0f video_403=%.0f | discovery_workers=%d max=%d",
             settings.bot_block_cooldown, settings.bot_block_backoff_factor,
             settings.bot_block_max_cooldown, settings.video_403_cooldown,
             settings.discovery_workers, settings.max_discovery_workers)
    log.info("RUNTIME_CONFIG eval_parallel=%d max_videos_per_channel=%d results_per_query=%d "
             "discovery_queries_per_run=%d ytdlp_delay=%.1f probe_min_interval=%.1f",
             settings.eval_parallel, settings.max_videos_per_channel,
             settings.results_per_query, settings.discovery_queries_per_run,
             settings.ytdlp_delay, settings.probe_min_interval)
    log.info("ACCESS_STATE snapshot: %s", access_controller.snapshot())
    _ensure_pot_server(settings)

    deadline = time.time() + max_hours * 3600
    results: list[dict] = []
    seen: set[str] = _load_checked(settings)  # уже проверенные каналы (переживает рестарт)

    # Переживаем рестарт: подхватываем уже найденные каналы (и не переоцениваем их).
    _rp = Path(settings.logs_dir) / "found_channels.json"
    if _rp.exists():
        try:
            _existing = json.loads(_rp.read_text(encoding="utf-8"))
            if isinstance(_existing, list) and _existing:
                results = _existing
                for r in results:
                    if r.get("channel_id"):
                        seen.add(r["channel_id"])
                log.info("Загружено %d ранее найденных каналов", len(results))
        except Exception as e:
            log.warning("Не удалось загрузить найденные каналы: %s", e)
    log.info("Пропущено как уже проверенные: %d каналов", len(seen))

    attempt = 0
    ytdlp_channels: list = []  # кэш discovery через yt-dlp (без квоты search)
    temp_retry: dict[str, float] = {}  # channel_id -> unixtime, когда можно повторить

    while time.time() < deadline and len(results) < target:
        attempt += 1
        log.info("--- Попытка %d: найдено %d/%d, осталось %.1f ч ---",
                 attempt, len(results), target, max(0.0, (deadline - time.time()) / 3600))

        # 0. ЕДИНЫЙ координатор паузы: пока cooldown активен, прямые запросы к youtube.com
        # (discovery/re-discovery/probe) не запускаются вообще.
        if access_controller.is_blocked():
            _wait_for_unblock(settings)
            if access_controller.is_blocked():
                continue
        # 0b. Recovery check: один ограниченный запрос перед возобновлением.
        if access_controller.state in (access_controller.BLOCKED, access_controller.RECOVERING):
            if access_controller.start_recovery_probe():
                if not recovery_probe():
                    log.warning("RECOVERY_PROBE_BLOCKED — остаёмся в cooldown")
                    _wait_for_unblock(settings)
                    continue
                access_controller.mark_recovery_succeeded()

        _ensure_pot_server(settings)

        youtube = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
        mimo = create_vision_client(settings)
        sampler = FrameSampler(
            settings.ffmpeg_path, settings.ffprobe_path, Path(settings.temp_dir),
            delay=settings.ytdlp_delay, cookies_file=settings.cookies_file,
            format_mode=settings.video_format_mode,
            frame_extraction_mode=settings.frame_extraction_mode,
            video_error_cooldown_sec=settings.video_403_cooldown,
            probe_min_interval=settings.probe_min_interval,
        )
        evaluator = ChannelEvaluator(settings, youtube, mimo, sampler, parallel=settings.eval_parallel)

        # PRIMARY: yt-dlp discovery (бесплатно, 165 локальных запросов, без квоты search).
        if not ytdlp_channels:
            try:
                ytdlp_channels = discover_channels_ytdlp(settings, youtube)
            except Exception as e:
                log.warning("yt-dlp discovery ошибка: %s", e)
        channels = list(ytdlp_channels)

        # SUPPLEMENT: Data API search (если есть квота search) — дополняет каналы.
        # Запросы: YAML + ротируемая выборка из пула (больше API, меньше yt-dlp).
        api_queries = _api_query_batch(settings)
        try:
            api_channels = find_channels(youtube, settings, api_queries)
            known = {c.channel_id for c in channels}
            extra = [c for c in api_channels if c.channel_id not in known]
            if extra:
                log.info("Data API search добавил %d новых каналов", len(extra))
                channels.extend(extra)
        except YouTubeQuotaError:
            pass
        except Exception as e:
            log.warning("Data API search: %s", e)

        if not channels:
            log.info("Нет каналов. Жду %d c...", quota_wait)
            _save(results, settings)
            time.sleep(quota_wait)
            continue

        log.info("Кандидатов: %d (quota_used=%d)", len(channels), youtube.quota_used)

        # --- Re-discovery при исчерпании очереди (не ждём полного нуля) ---
        usable = [c for c in channels
                  if c.channel_id not in seen
                  and time.time() >= temp_retry.get(c.channel_id, 0.0)]
        if len(usable) < settings.rediscovery_queue_threshold:
            log.info("Очередь исчерпана (usable=%d < %d) — re-discovery...",
                     len(usable), settings.rediscovery_queue_threshold)
            try:
                fresh_batch = discover_channels_ytdlp(settings, youtube)
                # Больше Data API, меньше yt-dlp: API не ловит bot-check.
                # Дополняем re-discovery кандидатами из API (если есть квота).
                try:
                    api_batch = find_channels(youtube, settings, api_queries)
                    if api_batch:
                        fresh_batch = list(fresh_batch) + list(api_batch)
                        log.info("Re-discovery: API добавил %d кандидатов", len(api_batch))
                except YouTubeQuotaError:
                    log.info("Re-discovery: квота Data API исчерпана, только yt-dlp")
                except Exception as e:
                    log.warning("Re-discovery API: %s", e)
                fresh = [c for c in fresh_batch if c.channel_id not in seen]
                if fresh:
                    ytdlp_channels = fresh_batch
                    channels = list(fresh_batch)
                    log.info("Re-discovery: %d новых кандидатов", len(fresh))
                else:
                    log.info("Re-discovery: 0 новых, пауза %d c", int(settings.rediscovery_cooldown))
                    time.sleep(settings.rediscovery_cooldown)
                    continue
            except Exception as e:
                log.warning("Re-discovery ошибка: %s", e)
                time.sleep(settings.rediscovery_cooldown)
                continue

        progressed = False
        last_metrics_log = time.time()
        for ch in channels:
            if time.time() >= deadline or len(results) >= target:
                break
            if ch.channel_id in seen:
                continue
            if time.time() < temp_retry.get(ch.channel_id, 0.0):
                continue  # канал в per-video cooldown после temp-ошибки
            if is_bot_blocked():
                remaining = access_controller.cooldown_remaining() or settings.bot_block_cooldown
                log.info("bot-block активен (%d c) — прекращаем обработку очереди, "
                         "пауза в едином координаторе", int(remaining))
                break  # каналы не помечены seen -> будут повторены после cooldown
            try:
                pool, ratio = get_channel_video_pool(youtube, ch, settings)
                if ratio > settings.max_shorts_ratio:
                    _mark_seen(seen, settings, ch.channel_id)
                    continue
                if not pool:
                    _mark_seen(seen, settings, ch.channel_id)
                    continue
                ev = evaluator.evaluate(ch, pool)
                if is_bot_blocked():
                    # Блок сработал во время оценки — не помечаем seen, откладываем канал.
                    remaining = access_controller.cooldown_remaining() or settings.bot_block_cooldown
                    temp_retry[ch.channel_id] = time.time() + remaining
                    log.info("bot-block при оценке %s, отложен на %d c...", ch.title, int(remaining))
                    break
                if ev.temp_error:
                    # Временные ошибки (403/expire/network) — НЕ считаем канал проверенным.
                    temp_retry[ch.channel_id] = time.time() + settings.video_403_cooldown
                    log.info("temp-ошибка у %s (%s), отложен на %d c",
                             ch.title, ev.reason, int(settings.video_403_cooldown))
                    continue
                _mark_seen(seen, settings, ch.channel_id)
                progressed = True
                if ev.passed:
                    results.append(_summarize(ch, ev))
                    log.info(">>> НАЙДЕН #%d: %s | %s", len(results), ch.channel_url, ev.best_category)
                    _save(results, settings)
                if time.time() - last_metrics_log >= 600:
                    log.info("METRICS %s", access_metrics.summary())
                    last_metrics_log = time.time()
            except YouTubeQuotaError:
                log.warning("Quota исчерпана при оценке. Жду %d c...", quota_wait)
                _save(results, settings)
                time.sleep(quota_wait)
                break
            except Exception as e:
                kind = classify_youtube_error(str(e))
                if kind in TEMP_KINDS:
                    # Временная ошибка — НЕ помечаем как проверенный, откладываем.
                    temp_retry[ch.channel_id] = time.time() + settings.video_403_cooldown
                    log.warning("Канал %s: [%s] временная ошибка (не помечаю checked): %s",
                                ch.title, kind, str(e)[:160])
                else:
                    _mark_seen(seen, settings, ch.channel_id)
                    log.warning("Канал %s: [%s] %s", ch.title, kind, str(e)[:160])

        if len(results) < target:
            if not progressed:
                log.info("Кандидаты исчерпаны; повторный поиск через 300 c")
                time.sleep(300)
            else:
                time.sleep(5)

    _save(results, settings)
    log.info("=== OVERNIGHT ЗАВЕРШЁН: найдено %d каналов ===", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
