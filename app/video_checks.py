"""Технические проверки видео: ffmpeg/ffprobe + Pillow/numpy.

Временные признаки (статичность/смена ракурса) оцениваются ТЕХНИЧЕСКИ по серии
уменьшенных кадров, а не по 5 кадрам для MiMo.
"""
from __future__ import annotations

import subprocess

from . import subproc
from pathlib import Path

import numpy as np
from PIL import Image

from .logger import get_logger
from .models import TechnicalResult, VideoMeta

log = get_logger("checks")


def run_ffmpeg(ffmpeg_path: str, args: list[str], timeout: int = 120) -> tuple[int, bytes, bytes]:
    proc = subproc.run(
        [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def image_dark_fraction(path: Path) -> float:
    """Доля тёмных пикселей (яркость < 60) в кадре. 0.0..1.0."""
    try:
        img = Image.open(path).convert("L")
        img.thumbnail((256, 256))
        arr = np.asarray(img, dtype=np.float32)
        return float((arr < 60).mean())
    except Exception as e:
        log.warning("Не удалось оценить яркость %s: %s", path, e)
        return 0.0


def detect_static(
    ffmpeg_path: str,
    meta: VideoMeta,
    temp_dir: Path,
    sample_frames: int = 12,
    scale_width: int = 200,
    threshold: float = 0.02,
) -> tuple[bool, float]:
    """Оценивает статичность по серии уменьшенных кадров, распределённых по всей длительности.

    Возвращает (is_static, motion_score). motion_score — средняя межкадровая разница
    в диапазоне примерно 0..1 (чем меньше, тем статичнее).
    """
    if meta.duration <= 0 or not meta.media_url:
        return False, 1.0

    n = max(3, min(sample_frames, int(meta.duration) + 1))
    times: list[float] = []
    for i in range(n):
        frac = (i + 0.5) / n
        t = round(min(max(0.5, meta.duration - 0.5), meta.duration * frac), 2)
        if times and abs(t - times[-1]) < 0.5:
            continue
        times.append(t)

    out_dir = temp_dir / f"static_{meta.video_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames: list[Path] = []
    for i, t in enumerate(times):
        out = out_dir / f"s{i:02d}.jpg"
        code, _, _ = run_ffmpeg(ffmpeg_path, [
            "-ss", f"{t:.2f}", "-i", meta.media_url,
            "-vf", f"scale={scale_width}:-2",
            "-frames:v", "1", "-q:v", "5",
            str(out),
        ])
        if code == 0 and out.exists():
            frames.append(out)

    if len(frames) < 2:
        return False, 1.0

    grays: list[np.ndarray] = []
    for p in frames:
        try:
            img = Image.open(p).convert("L")
            grays.append(np.asarray(img, dtype=np.float32))
        except Exception:
            continue
    if len(grays) < 2:
        return False, 1.0

    # Нормализуем размеры (у разных источников может отличаться высота после scale).
    base = grays[0].shape
    norm: list[np.ndarray] = []
    for g in grays:
        if g.shape != base:
            g = np.asarray(Image.fromarray(g.astype("uint8")).resize((base[1], base[0])), dtype=np.float32)
        norm.append(g)

    diffs: list[float] = []
    for a, b in zip(norm, norm[1:]):
        diffs.append(float(np.abs(a - b).mean() / 255.0))
    motion = float(np.mean(diffs)) if diffs else 1.0
    return motion < threshold, motion


def check_technical(settings, meta: VideoMeta) -> TechnicalResult:
    """Итоговая техническая проверка видео."""
    if meta.duration <= 0 or meta.width <= 0 or meta.height <= 0:
        return TechnicalResult(ok=False, error="нет метаданных (недоступно/удалено/private)")

    is_vertical = meta.height > meta.width
    resolution_ok = meta.height >= settings.min_height

    reasons: list[str] = []
    ok = True
    if is_vertical:
        ok = False
        reasons.append("вертикальное видео")
    if not resolution_ok:
        ok = False
        reasons.append(f"разрешение {meta.width}x{meta.height} < {settings.min_height}p")
    if meta.duration <= settings.min_duration_seconds:
        ok = False
        reasons.append(f"длительность {meta.duration:.1f}s <= {settings.min_duration_seconds}s")

    # Верхнего ограничения длительности НЕТ (правило проекта: >5 c допустимо).
    # Для видео >30 мин ниже выполняется проверка статичности.

    is_static = False
    motion = 1.0
    if ok and meta.duration >= settings.static_min_duration:
        is_static, motion = detect_static(
            settings.ffmpeg_path, meta, Path(settings.temp_dir),
            settings.static_sample_frames, settings.static_scale_width,
            settings.static_motion_threshold,
        )
        if is_static:
            ok = False
            reasons.append(f"статичное видео, ракурс не меняется (motion={motion:.4f})")

    return TechnicalResult(
        ok=ok,
        width=meta.width,
        height=meta.height,
        duration=meta.duration,
        is_vertical=is_vertical,
        resolution_ok=resolution_ok,
        is_static=is_static,
        motion_score=motion,
        error="; ".join(reasons),
    )
