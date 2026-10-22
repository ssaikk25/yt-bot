# -*- coding: utf-8 -*-
"""Изолированная диагностика доступа к YouTube (БЕЗ массового discovery).

Запуск:  python -m app.access_diagnose [video_id]

Что проверяет (по одному контролируемому запросу каждого типа):
  A. Data API  (www.googleapis.com) — независимость от bot-check;
  B. ytsearch  (youtube.com)        — прямой поиск через yt-dlp;
  C. probe     (youtube.com)        — метаданные/поток одного видео;
  D. formats   (youtube.com)        — доступные форматы для того же видео;
  E. frame     (googlevideo.com)    — один кадр ffmpeg (только если C дал поток).

Правила:
  - не запускается при активном cooldown;
  - при явном bot-check немедленно прекращает прямые запросы к youtube.com;
  - не печатает cookies/токены/подписанные media_url; только метаданные.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .access_control import (
    BOT_CHECK,
    BotBlockedError,
    access_controller,
    classify_youtube_error,
    configure_access_control,
)
from .config import load_settings
from .discovery import _yt_search
from .frame_sampler import FrameSampler, ProbeError
from .logger import get_logger, setup_logging
from .youtube_client import YouTubeClient, YouTubeQuotaError

log = get_logger("diagnose")

DEFAULT_VIDEO = "dQw4w9WgXcQ"


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    video_id = argv[0] if argv else DEFAULT_VIDEO
    settings = load_settings()
    setup_logging(Path(settings.logs_dir))
    configure_access_control(
        settings.bot_block_cooldown,
        settings.bot_block_backoff_factor,
        settings.bot_block_max_cooldown,
        settings.video_403_cooldown,
        state_path=Path(settings.logs_dir) / "access_state.json",
    )
    access_controller.load_state()

    out_path = Path(settings.logs_dir) / "access_diagnose.jsonl"
    rows: list[dict] = []

    def rec(component: str, host: str, result: str, kind: str = "",
            duration: float = 0.0, detail: str = "") -> dict:
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "component": component,
            "host": host,
            "result": result,
            "kind": kind,
            "duration_s": round(duration, 2),
            "detail": detail[:160],
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        return row

    with out_path.open("a", encoding="utf-8") as fh:
        def flush() -> None:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.clear()

        # Предохранитель: при активном cooldown прямые запросы не запускаем.
        if access_controller.is_blocked():
            rec("precheck", "-", "skipped", "cooldown",
                0.0, f"cooldown {int(access_controller.cooldown_remaining())} c")
            flush()
            return 2

        # A. Data API -----------------------------------------------------
        try:
            t0 = time.time()
            client = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
            details = client.get_video_details([video_id])
            rec("data_api", "www.googleapis.com", "ok", "",
                time.time() - t0, f"items={len(details)} quota_used={client.quota_used}")
        except YouTubeQuotaError as e:
            rec("data_api", "www.googleapis.com", "fail", "api_quota", 0.0, str(e)[:120])
        except Exception as e:  # noqa: BLE001
            rec("data_api", "www.googleapis.com", "fail",
                classify_youtube_error(str(e), source="api"), 0.0, str(e)[:120])
        flush()

        # B. ytsearch -----------------------------------------------------
        try:
            t0 = time.time()
            entries = _yt_search("hand forge blacksmith", 1)
            rec("ytsearch", "youtube.com", "ok", "", time.time() - t0,
                f"entries={len(entries)}")
        except BotBlockedError:
            rec("ytsearch", "youtube.com", "skipped", "cooldown", 0.0, "")
            flush()
            return 2
        except Exception as e:  # noqa: BLE001
            kind = classify_youtube_error(str(e), source="youtube")
            rec("ytsearch", "youtube.com", "fail", kind, 0.0, str(e)[:120])
            flush()
            if kind == BOT_CHECK:
                return 1
        flush()

        # C. probe --------------------------------------------------------
        url = f"https://www.youtube.com/watch?v={video_id}"
        sampler = FrameSampler(
            settings.ffmpeg_path, settings.ffprobe_path, Path(settings.temp_dir),
            delay=0.0, cookies_file=settings.cookies_file,
            format_mode=settings.video_format_mode,
            frame_extraction_mode=settings.frame_extraction_mode,
            video_error_cooldown_sec=settings.video_403_cooldown,
        )
        meta = None
        try:
            t0 = time.time()
            meta = sampler.probe(url, video_id)
            rec("probe", "youtube.com", "ok", "", time.time() - t0,
                f"h={meta.height} dur={int(meta.duration)}")
        except ProbeError as e:
            rec("probe", "youtube.com", "fail", e.kind, 0.0, str(e)[:120])
            flush()
            if e.kind == BOT_CHECK:
                return 1
        flush()

        # D. formats ------------------------------------------------------
        try:
            import yt_dlp
            t0 = time.time()
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                   "skip_download": True, "noplaylist": True,
                                   "socket_timeout": 30}) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = info.get("formats") or []
            video_fmts = [f for f in fmts if f.get("vcodec") not in (None, "none")]
            rec("formats", "youtube.com", "ok" if video_fmts else "empty", "",
                time.time() - t0, f"formats={len(fmts)} video={len(video_fmts)}")
        except Exception as e:  # noqa: BLE001
            kind = classify_youtube_error(str(e), source="youtube")
            rec("formats", "youtube.com", "fail", kind, 0.0, str(e)[:120])
            flush()
            if kind == BOT_CHECK:
                return 1
        flush()

        # E. frame (googlevideo) -----------------------------------------
        if meta is None or not meta.media_url:
            rec("frame", "googlevideo.com", "skipped", "no_media", 0.0, "")
            flush()
            return 0
        try:
            t0 = time.time()
            frames = sampler.extract_frames(meta, [min(5.0, max(1.0, meta.duration / 2))], "diag")
            rec("frame", "googlevideo.com", "ok" if frames else "empty", "",
                time.time() - t0, f"frames={len(frames)}")
        except Exception as e:  # noqa: BLE001
            kind = classify_youtube_error(str(e), source="googlevideo")
            rec("frame", "googlevideo.com", "fail", kind, 0.0, str(e)[:120])
        flush()
        return 0


if __name__ == "__main__":
    sys.exit(main())
