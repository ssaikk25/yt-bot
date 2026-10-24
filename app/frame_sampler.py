# -*- coding: utf-8 -*-
"""Извлечение метаданных и отдельных кадров видео через yt-dlp + ffmpeg.

Важно: кадры извлекаются ТОЛЬКО как отдельные изображения (JPEG/PNG/WebP).
Целое видео в MiMo никогда не отправляется.

Ключевые свойства реализации:
- ОДИН probe (yt-dlp) на видео, далее media_url переиспользуется для всех кадров;
- классификация ошибок (bot-check / age-gate / unavailable / 403 / expire / network);
- глобальный state machine + exponential backoff, per-video cooldown для 403;
- учёт `expire` из media_url (не используем протухший URL);
- опциональное извлечение нескольких кадров ОДНИМ ffmpeg-процессом (multi).
"""
from __future__ import annotations

import subprocess

from . import subproc
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from .access_control import (
    BOT_CHECK,
    FORBIDDEN,
    GVS_403,
    NETWORK,
    OTHER_403,
    PAGE_403,
    URL_EXPIRED,
    BotBlockedError,
    access_controller,
    access_metrics,
    classify_youtube_error,
    video_cooldown,
)
from .logger import get_logger
from .models import VideoMeta

log = get_logger("sampler")


class ProbeError(RuntimeError):
    """Ошибка probe (yt-dlp). `kind` — один из кодов access_control."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class MediaUrlError(RuntimeError):
    """Ошибка извлечения кадров из media_url (ffmpeg/CDN)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _format_selector(mode: str, min_height: int) -> str:
    """Собирает yt-dlp format selector по режиму.

    mode="720"      -> приоритет 720p (меньше network), fallback 1080p.
    mode="720_1080" -> текущий: 720..1080p (аудио не нужно).
    """
    if mode == "720":
        return (f"bestvideo[height>={min_height}][height<=720]"
                f"/bestvideo[height>={min_height}][height<=1080]"
                f"/bestvideo[height>={min_height}]/bestvideo")
    return (f"bestvideo[height>={min_height}][height<=1080]"
            f"/bestvideo[height>={min_height}]/bestvideo")


def _extract_url_expiry(media_url: str) -> float:
    """Достаёт unixtime истечения из параметра `expire` ссылки googlevideo."""
    try:
        q = parse_qs(urlparse(media_url).query)
        expire = q.get("expire", [None])[0]
        if expire:
            return float(expire)
    except Exception:
        pass
    return 0.0


# Обратная совместимость: overnight импортирует is_bot_blocked из frame_sampler.
def is_bot_blocked() -> bool:
    return access_controller.is_blocked()


# Глобальный (на процесс) троттлинг probe: суммарный интервал между запросами
# к YouTube для ВСЕХ потоков EVAL_PARALLEL. Защита от «залповой» скорости,
# после которой YouTube выдаёт bot-check.
_probe_gate_lock = threading.Lock()
_probe_last_mono = 0.0


def _mark_bot_block() -> None:
    access_controller.note_bot_check()


def compute_timestamps(
    duration: float,
    frame_count: int,
    fractions: list[float],
    short_threshold: float = 30.0,
) -> list[float]:
    """Вычисляет точки времени для кадров.

    - Обычное видео: около начала / 25% / 50% / 75% / конца.
    - Короткое видео: равномерно распределённые точки без дубликатов.
    """
    if duration <= 0 or frame_count <= 0:
        return []
    if duration >= short_threshold:
        times = [duration * f for f in fractions[:frame_count]]
    else:
        step = duration / (frame_count + 1)
        times = [step * (i + 1) for i in range(frame_count)]

    margin = min(0.5, duration / 10)
    cleaned: list[float] = []
    for t in times:
        t = round(max(margin, min(duration - margin, t)), 2)
        if cleaned and abs(t - cleaned[-1]) < 0.3:
            continue  # защита от дубликатов
        cleaned.append(t)
    return cleaned


class FrameSampler:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        temp_dir: Path = Path("./temp"),
        delay: float = 0.0,
        cookies_file: str = "",
        format_mode: str = "720_1080",
        frame_extraction_mode: str = "single",
        video_error_cooldown_sec: float = 600.0,
        probe_min_interval: float = 0.0,
    ):
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self.temp_dir = temp_dir
        self.delay = delay
        self.cookies_file = cookies_file
        self.format_mode = format_mode
        self.frame_extraction_mode = frame_extraction_mode  # "single" | "multi"
        self.video_error_cooldown_sec = video_error_cooldown_sec
        self.probe_min_interval = max(0.0, float(probe_min_interval or 0.0))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _global_probe_throttle(self) -> None:
        """ГЛОБАЛЬНЫЙ лимит частоты probe (общий на все потоки EVAL_PARALLEL).

        Нужен, чтобы суммарная скорость не превышала устойчивую для IP
        (иначе YouTube выдаёт bot-check после ~500 видео/час).
        """
        if self.probe_min_interval <= 0:
            return
        global _probe_last_mono
        while True:
            with _probe_gate_lock:
                now = time.monotonic()
                wait = _probe_last_mono + self.probe_min_interval - now
                if wait <= 0:
                    _probe_last_mono = now
                    return
            time.sleep(min(wait, 1.0))

    # ------------------------------------------------------------------ #
    def probe(self, video_url: str, video_id: str, min_height: int = 720) -> VideoMeta:
        """Получает прямую ссылку на медиапоток и метаданные без скачивания видео.

        Вызывается ОДИН раз на видео. Дальше media_url переиспользуется.
        """
        if self.delay > 0:
            time.sleep(self.delay)
        # Глобальный лимит частоты probe (общий для всех потоков EVAL_PARALLEL).
        self._global_probe_throttle()
        try:
            access_controller.gate("probe")
        except BotBlockedError as e:
            raise ProbeError(BOT_CHECK, str(e)) from e
        if video_cooldown.is_blocked(video_id):
            raise ProbeError(GVS_403, "per-video cooldown активен (недавний 403)")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 30,  # против зависания на сетевых операциях YouTube
            "format": _format_selector(self.format_mode, min_height),
        }
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        t0 = time.time()
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
        except Exception as e:
            kind = classify_youtube_error(str(e), source="youtube")
            if kind == BOT_CHECK:
                access_controller.note_bot_check("probe")
            elif kind in (PAGE_403, OTHER_403):
                access_controller.note_suspected(f"probe {kind}")
            elif kind == GVS_403:
                video_cooldown.mark(video_id)
                log.warning("GVS_FORBIDDEN video=%s (медиапоток, не глобальный блок)", video_id)
            access_metrics.record_probe(False, kind, time.time() - t0)
            log.warning("probe %s: [%s] %s", video_id, kind, str(e)[:160])
            raise ProbeError(kind, str(e)) from e

        if not info:
            access_metrics.record_probe(False, "empty", time.time() - t0)
            raise ProbeError("empty", "yt-dlp не вернул метаданные")

        duration = float(info.get("duration") or 0)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        media_url = info.get("url") or ""
        ext = info.get("ext") or "mp4"
        if not media_url:
            for f in info.get("formats") or []:
                if f.get("url"):
                    media_url = f["url"]
                    break

        access_controller.note_success()
        access_metrics.record_probe(True, "", time.time() - t0)

        now = time.time()
        expire = _extract_url_expiry(media_url)
        return VideoMeta(
            video_id=video_id,
            duration=duration,
            width=width,
            height=height,
            media_url=media_url,
            ext=ext,
            media_url_created_at=now,
            media_url_expire=expire,
        )

    # ------------------------------------------------------------------ #
    def extract_frames(
        self,
        meta: VideoMeta,
        timestamps: list[float],
        prefix: str,
        quality: int = 3,
        fmt: str = "jpg",
        max_width: int = 0,
    ) -> list[Path]:
        """Извлекает кадры в заданные моменты времени.

        Если media_url протух (expire в прошлом) — поднимает MediaUrlError(URL_EXPIRED).
        Если ffmpeg падает на 403/expire/network — поднимает MediaUrlError.
        Иначе возвращает список извлечённых кадров (может быть неполным).
        """
        if not meta.media_url:
            log.warning("Нет media_url для %s", meta.video_id)
            return []

        if meta.media_url_expired:
            raise MediaUrlError(URL_EXPIRED, "media_url истёк (expire в прошлом)")

        out_dir = self.temp_dir / prefix
        out_dir.mkdir(parents=True, exist_ok=True)

        if self.frame_extraction_mode == "multi" and len(timestamps) > 1:
            paths = self._extract_frames_multi(meta, timestamps, out_dir, quality, fmt, max_width)
        else:
            paths = self._extract_frames_single(meta, timestamps, out_dir, quality, fmt, max_width)
        return paths

    # ------------------------------------------------------------------ #
    def _cached(self, out_dir: Path, timestamps: list[float], fmt: str) -> list[tuple[int, Path]]:
        cached: list[tuple[int, Path]] = []
        for i, t in enumerate(timestamps):
            out = out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}"
            if out.exists() and out.stat().st_size > 500:
                cached.append((i, out))
        return cached

    def _vf(self, max_width: int) -> list[str]:
        if max_width and max_width > 0:
            return ["-vf", f"scale='min({max_width},iw)':-2"]
        return []

    def _classify_ffmpeg_failure(self, stderr: bytes) -> str:
        # ffmpeg тянет медиапоток с googlevideo.com — отдельный источник ошибок.
        return classify_youtube_error((stderr or b"").decode("utf-8", "replace"),
                                      source="googlevideo")

    def _note_frame_result(self, ok: bool, duration: float, kind: str = "") -> None:
        access_metrics.record_frame(ok, duration, kind)

    # ------------------------------------------------------------------ #
    def _extract_frames_single(self, meta, timestamps, out_dir, quality, fmt, max_width) -> list[Path]:
        """Один ffmpeg-процесс на кадр (надёжнее, но больше процессов)."""
        paths: list[Path] = []
        t0 = time.time()
        for i, t in enumerate(timestamps):
            out = out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}"
            if out.exists() and out.stat().st_size > 500:
                paths.append(out)
                continue
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", meta.media_url,
            ]
            cmd += self._vf(max_width)
            # yuvj420p (full-range) — канонический формат для JPEG.
            cmd += ["-pix_fmt", "yuvj420p", "-frames:v", "1", "-q:v", str(quality), str(out)]
            st0 = time.time()
            try:
                subproc.run(cmd, check=True, timeout=60, capture_output=True)
            except subprocess.TimeoutExpired:
                self._note_frame_result(False, time.time() - st0, "timeout")
                log.warning("ffmpeg timeout при извлечении кадра t=%ss (%s)", t, meta.video_id)
                continue
            except subprocess.CalledProcessError as e:
                kind = self._classify_ffmpeg_failure(e.stderr or b"")
                self._note_frame_result(False, time.time() - st0, kind)
                log.warning("ffmpeg ошибка при t=%ss (%s) [%s]: %s",
                            t, meta.video_id, kind,
                            (e.stderr or b"").decode("utf-8", "replace")[:200])
                if kind in (GVS_403, OTHER_403, URL_EXPIRED, NETWORK) and not paths:
                    # первая же ошибка доступа — пробрасываем наверх для re-probe
                    raise MediaUrlError(kind, f"ffmpeg {kind} при t={t}s")
                continue
            if out.exists() and out.stat().st_size > 500:
                paths.append(out)
        if paths:
            self._note_frame_result(True, time.time() - t0)
        return paths

    # ------------------------------------------------------------------ #
    def _extract_frames_multi(self, meta, timestamps, out_dir, quality, fmt, max_width) -> list[Path]:
        """Несколько кадров ОДНИМ ffmpeg-процессом (несколько -i с -ss)."""
        missing = [(i, t) for i, t in enumerate(timestamps)
                   if not (out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}").exists()
                   or (out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}").stat().st_size <= 500]

        cached = self._cached(out_dir, timestamps, fmt)
        paths = [p for _, p in cached]
        if not missing:
            return paths

        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for _, t in missing:
            cmd += ["-ss", f"{t:.2f}", "-i", meta.media_url]
        for j, (i, t) in enumerate(missing):
            out = out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}"
            cmd += ["-map", f"{j}:v:0"]
            cmd += self._vf(max_width)
            cmd += ["-pix_fmt", "yuvj420p", "-frames:v", "1", "-q:v", str(quality), str(out)]

        t0 = time.time()
        try:
            subproc.run(cmd, check=True, timeout=90, capture_output=True)
        except subprocess.TimeoutExpired:
            self._note_frame_result(False, time.time() - t0, "timeout")
            log.warning("ffmpeg multi timeout (%s)", meta.video_id)
        except subprocess.CalledProcessError as e:
            kind = self._classify_ffmpeg_failure(e.stderr or b"")
            self._note_frame_result(False, time.time() - t0, kind)
            log.warning("ffmpeg multi ошибка (%s) [%s]: %s",
                        meta.video_id, kind,
                        (e.stderr or b"").decode("utf-8", "replace")[:200])
            if kind in (GVS_403, OTHER_403, URL_EXPIRED, NETWORK) and not paths:
                raise MediaUrlError(kind, f"ffmpeg multi {kind}")

        for i, t in missing:
            out = out_dir / f"frame_{i:02d}_{int(t)}s.{fmt}"
            if out.exists() and out.stat().st_size > 500:
                paths.append(out)
        if paths:
            self._note_frame_result(True, time.time() - t0)
        return paths

    # ------------------------------------------------------------------ #
    def mark_video_temp_error(self, video_id: str) -> None:
        """Ставит per-video cooldown после 403/expire (не долбить видео)."""
        video_cooldown.mark(video_id)
