# -*- coding: utf-8 -*-
"""Дешёвое сравнение миниатюр (aHash) для channel-level farm fingerprint.

Не трогает video stream: качает только маленькие jpg-миниатюры из metadata
YouTube, хэширует (Pillow) и сравнивает Hamming-расстоянием. Есть локальный
кэш на диске (temp/thumbnails/) и в памяти.
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

from .logger import get_logger

log = get_logger("thumbhash")

HASH_SIZE = 8
_CACHE_MEM: dict[str, Optional[int]] = {}


def ahash_from_bytes(data: bytes, size: int = HASH_SIZE) -> int:
    img = Image.open(io.BytesIO(data)).convert("L")
    return ahash_from_image(img, size)


def ahash_from_image(img: Image.Image, size: int = HASH_SIZE) -> int:
    g = img.resize((size, size), Image.LANCZOS)
    px = list(g.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p >= avg else 0)
    return bits


def hamming(a: int, b: int, size: int = HASH_SIZE) -> float:
    """Нормализованное расстояние 0..1 (0 = идентичны)."""
    return bin(a ^ b).count("1") / (size * size)


def _cache_path(cache_dir: Path, video_id: str) -> Path:
    return cache_dir / f"{video_id}.hash"


def _load_hash(video_id: str, cache_dir: Path) -> Optional[int]:
    if video_id in _CACHE_MEM:
        return _CACHE_MEM[video_id]
    p = _cache_path(cache_dir, video_id)
    if p.exists():
        try:
            val = int(p.read_text(encoding="utf-8").strip())
            _CACHE_MEM[video_id] = val
            return val
        except Exception:
            pass
    return None


def _save_hash(video_id: str, value: Optional[int], cache_dir: Path) -> None:
    _CACHE_MEM[video_id] = value
    if value is None:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(cache_dir, video_id).write_text(str(value), encoding="utf-8")
    except Exception:
        pass


def thumbnails_similarity(
    videos,  # list[VideoCandidate] с .video_id и .thumbnail_url
    cache_dir: Path,
    sample: int = 10,
    timeout: float = 15.0,
) -> tuple[float, int]:
    """Среднее попарное сходство миниатюр (0..1) и число успешно скачанных.

    Падение одной миниатюры не ломает расчёт — пропускается.
    """
    hashes: list[int] = []
    ok = 0
    urls = [(v.video_id, getattr(v, "thumbnail_url", "") or "") for v in videos[:sample]]
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for vid, url in urls:
            if not url:
                continue
            cached = _load_hash(vid, cache_dir)
            if cached is not None:
                hashes.append(cached)
                ok += 1
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
                h = ahash_from_bytes(resp.content)
                _save_hash(vid, h, cache_dir)
                hashes.append(h)
                ok += 1
            except Exception as e:
                _save_hash(vid, None, cache_dir)
                log.warning("thumbnail %s: %s", vid, str(e)[:100])
    if len(hashes) < 2:
        return 0.0, ok
    sims = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            sims.append(1.0 - hamming(hashes[i], hashes[j]))
    return (sum(sims) / len(sims)), ok
