# -*- coding: utf-8 -*-
"""Benchmark доступа к YouTube-видеопотоку (probe + извлечение кадров).

Сравнивает конфигурации на ФИКСИРОВАННОЙ выборке видео (по умолчанию ~20):
  A: current         — 1 ffmpeg-процесс НА КАДР, format 720_1080 (текущее поведение)
  B: multi           — ОДИН ffmpeg-процесс на все кадры, format 720_1080
  C: api_prefilter   — batch videos.list -> skip sd/short, multi, format 720_1080
  D: 720p            — multi, приоритет 720p (меньше network)

НЕ вызывает MiMo, НЕ трогает TagMe, НЕ ищет каналы, НЕ скачивает видео целиком.
Запуск:
  python -m app.youtube_access_benchmark                # все режимы на выборке по умолчанию
  python -m app.youtube_access_benchmark --max 10       # ограничить выборку
  python -m app.youtube_access_benchmark --video-ids a,b,c
  python -m app.youtube_access_benchmark --modes A,B    # только выбранные режимы
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .access_control import (
    BOT_CHECK,
    FORBIDDEN,
    access_controller,
    access_metrics,
    configure_access_control,
)
from .config import load_settings
from .frame_sampler import FrameSampler, MediaUrlError, ProbeError, compute_timestamps
from .logger import get_logger

log = get_logger("bench")

# Стабильные, общедоступные видео (720p+, горизонтальные) — только для замера доступа.
DEFAULT_VIDEO_IDS = [
    "dQw4w9WgXcQ",  # Rick Astley
    "9bZkp7q19f0",  # PSY
    "kJQP7kiw5Fk",  # Despacito
    "RgKAFK5djSk",  # See You Again
    "OPf0YbXqDm0",  # Uptown Funk
    "e-ORhEE9VVg",  # Blank Space
    "hT_nvWreIhg",  # Counting Stars
    "60ItHLz5WEA",  # Faded
    "fJ9rUzIMcZQ",  # Bohemian Rhapsody
    "YQHsXMglC9A",  # Hello
    "CevxZvSJLk8",  # Roar
    "09R8_2nJtjg",  # Sugar
    "2Vv-BfVoq4g",  # Perfect
    "7wtfhZwyrcc",  # Believer
    "JGwWNGJdvx8",  # Shape of You
    "LPn0KFlbqX8",  # Chandelier
    "fRh_vgS2dFE",  # Sorry
    "0KSOMA3QBU0",  # Dark Horse
    "kXYiU_JCYtU",  # Numb
    "uelHwf8o7_U",  # Eminem Love The Way You Lie
]

FRAME_FRACTIONS = [0.10, 0.50, 0.90]


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * 0.95))]


def _summarize(results: list[dict]) -> dict:
    n = len(results)
    ok = sum(1 for r in results if r["probe_ok"])
    fr_ok = sum(1 for r in results if r["frame_ok"])
    times = [r["elapsed"] for r in results if r["probe_ok"]]
    kinds: dict[str, int] = {}
    for r in results:
        for k in r.get("kinds", []):
            kinds[k] = kinds.get(k, 0) + 1
    frames_req = sum(r["frames_requested"] for r in results)
    frames_got = sum(r["frames_received"] for r in results)
    ffmpeg_procs = sum(r["ffmpeg_processes"] for r in results)
    return {
        "videos": n,
        "probe_success_rate": round(ok / max(1, n), 3),
        "frame_success_rate": round(fr_ok / max(1, ok), 3),
        "bot_check": kinds.get(BOT_CHECK, 0),
        "forbidden_403": kinds.get(FORBIDDEN, 0),
        "kinds": kinds,
        "mean_s": round(sum(times) / max(1, len(times)), 2),
        "p95_s": round(_p95(times), 2),
        "frames_requested": frames_req,
        "frames_received": frames_got,
        "frame_yield": round(frames_got / max(1, frames_req), 3),
        "ffmpeg_processes": ffmpeg_procs,
        "est_googlevideo_conns": frames_req,  # каждый seek-кадр = отдельное соединение
    }


def _api_prefilter(settings, youtube, video_ids: list[str]) -> list[str]:
    """Возвращает video_ids, прошедшие дешёвый API-фильтр (не sd, не short)."""
    details = youtube.get_video_details(video_ids)
    kept: list[str] = []
    for vid in video_ids:
        d = details.get(vid)
        if not d:
            continue
        content = d.get("contentDetails", {})
        snippet = d.get("snippet", {})
        definition = content.get("definition", "")
        if definition == "sd":
            continue
        live = snippet.get("liveBroadcastContent", "none") in ("live", "upcoming")
        if live:
            continue
        kept.append(vid)
    return kept


def run_test(name: str, sampler, videos: list[str], settings, api_prefilter: bool = False,
             youtube=None) -> list[dict]:
    results: list[dict] = []
    if api_prefilter and youtube is not None:
        videos = _api_prefilter(settings, youtube, videos)
    for vid in videos:
        url = f"https://www.youtube.com/watch?v={vid}"
        t0 = time.time()
        r = {
            "video_id": vid, "probe_ok": False, "frame_ok": False,
            "elapsed": 0.0, "kinds": [], "frames_requested": 3,
            "frames_received": 0, "ffmpeg_processes": 0,
        }
        try:
            meta = sampler.probe(url, vid, settings.min_height)
        except ProbeError as e:
            r["elapsed"] = time.time() - t0
            r["kinds"].append(e.kind)
            results.append(r)
            continue
        r["probe_ok"] = True
        ts = compute_timestamps(meta.duration, 3, FRAME_FRACTIONS, settings.short_video_threshold)
        r["frames_requested"] = len(ts)
        r["ffmpeg_processes"] = len(ts) if sampler.frame_extraction_mode == "single" else (1 if ts else 0)
        try:
            frames = sampler.extract_frames(
                meta, ts, prefix=f"bench_{vid}",
                quality=settings.frame_quality, fmt=settings.frame_format,
                max_width=settings.frame_max_width,
            )
        except MediaUrlError as e:
            r["elapsed"] = time.time() - t0
            r["kinds"].append(e.kind)
            results.append(r)
            continue
        r["frames_received"] = len(frames)
        r["frame_ok"] = len(frames) >= 2
        r["elapsed"] = time.time() - t0
        results.append(r)
    log.info("TEST %s: %d видео обработано", name, len(results))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark доступа к YouTube-потоку")
    ap.add_argument("--video-ids", default="", help="список video_id через запятую")
    ap.add_argument("--max", type=int, default=0, help="ограничить выборку первыми N")
    ap.add_argument("--modes", default="A,B,C,D", help="режимы: A,B,C,D")
    ap.add_argument("--json", action="store_true", help="вывести JSON")
    args = ap.parse_args()

    settings = load_settings()
    configure_access_control(
        settings.bot_block_cooldown, settings.bot_block_backoff_factor,
        settings.bot_block_max_cooldown, settings.video_403_cooldown,
    )

    if args.video_ids:
        videos = [v.strip() for v in args.video_ids.split(",") if v.strip()]
    else:
        videos = list(DEFAULT_VIDEO_IDS)
    if args.max and args.max > 0:
        videos = videos[: args.max]

    modes = [m.strip().upper() for m in args.modes.split(",") if m.strip()]
    configs = {
        "A": ("current", dict(frame_extraction_mode="single", format_mode="720_1080"), False),
        "B": ("multi", dict(frame_extraction_mode="multi", format_mode="720_1080"), False),
        "C": ("api_prefilter+multi", dict(frame_extraction_mode="multi", format_mode="720_1080"), True),
        "D": ("720p+multi", dict(frame_extraction_mode="multi", format_mode="720"), False),
    }

    print(f"=== YOUTUBE ACCESS BENCHMARK ({len(videos)} видео) ===")
    print(f"bot-block активен: {access_controller.is_blocked()} "
          f"(cooldown {int(access_controller.cooldown_remaining())} c)")
    print()

    reports: dict[str, dict] = {}
    for mode in modes:
        if mode not in configs:
            continue
        name, kw, api_pre = configs[mode]
        sampler = FrameSampler(
            settings.ffmpeg_path, settings.ffprobe_path, Path(settings.temp_dir),
            delay=settings.ytdlp_delay, cookies_file=settings.cookies_file,
            video_error_cooldown_sec=settings.video_403_cooldown,
            **kw,
        )
        from .youtube_client import YouTubeClient
        youtube = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
        results = run_test(f"{mode}: {name}", sampler, videos, settings,
                           api_prefilter=api_pre, youtube=youtube)
        reports[mode] = _summarize(results)

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        header = (f"{'mode':20s} {'probe_ok':>8s} {'frame_ok':>8s} {'bot':>4s} {'403':>4s} "
                  f"{'mean_s':>7s} {'p95_s':>7s} {'fr_yield':>8s} {'ffprocs':>8s} {'gv_conns':>8s}")
        print(header)
        print("-" * len(header))
        for mode in modes:
            if mode not in reports:
                continue
            s = reports[mode]
            print(f"{mode:20s} {s['probe_success_rate']:>8.3f} {s['frame_success_rate']:>8.3f} "
                  f"{s['bot_check']:>4d} {s['forbidden_403']:>4d} "
                  f"{s['mean_s']:>7.2f} {s['p95_s']:>7.2f} {s['frame_yield']:>8.3f} "
                  f"{s['ffmpeg_processes']:>8d} {s['est_googlevideo_conns']:>8d}")

    print("\n=== ACCESS METRICS (кумулятивно) ===")
    print(json.dumps(access_metrics.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
