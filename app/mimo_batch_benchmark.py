# -*- coding: utf-8 -*-
"""Benchmark MiMo micro-batch: 1 видео/запрос vs 2 видео/запрос.

Использует РЕАЛЬНЫЕ кэшированные кадры из temp/ (без сети к YouTube — кадры уже
на диске). Сравнивает суммарное время и время на одно видео.

Запуск: python -m app.mimo_batch_benchmark [--videos N]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import load_settings
from .mimo_client import MiMoClient

log_path = None


def load_videos(temp_dir: Path, n: int) -> list[list[bytes]]:
    """Берёт n наборов по 3 кэшированных кадра из temp/."""
    videos: list[list[bytes]] = []
    for d in temp_dir.iterdir():
        if not d.is_dir() or d.name.startswith(("static_", "bench_", "_")):
            continue
        if d.name.endswith("_deep") or d.name == "thumbnails":
            continue
        frames = sorted(d.glob("frame_*.jpg"))
        if len(frames) >= 3:
            imgs = [f.read_bytes() for f in frames[:3]]
            videos.append(imgs)
            if len(videos) >= n:
                break
    return videos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=int, default=4, help="сколько видео (чётное)")
    args = ap.parse_args()
    n = args.videos if args.videos % 2 == 0 else args.videos - 1
    if n < 2:
        n = 2

    settings = load_settings()
    temp_dir = Path(settings.temp_dir)
    videos = load_videos(temp_dir, n)
    if len(videos) < n:
        print(f"Недостаточно кэшированных кадров: найдено {len(videos)} из {n}")
        return 1

    client = MiMoClient(
        api_key=settings.mimo_api_key, base_url=settings.mimo_base_url,
        model=settings.mimo_model, categories=settings.categories,
        timeout=settings.mimo_timeout, retries=settings.mimo_retries,
        max_tokens=settings.mimo_max_tokens, temperature=settings.mimo_temperature,
        enable_thinking=settings.mimo_enable_thinking,
    )

    print(f"=== MIMO BATCH BENCHMARK: {n} видео, по 3 кадра ===")

    # TEST A: одиночные запросы
    t0 = time.time()
    single_results = []
    for v in videos:
        single_results.append(client.analyze(v))
    single_time = time.time() - t0
    single_per_video = single_time / n
    print(f"A: {n} одиночных запросов: {single_time:.1f}s total, {single_per_video:.1f}s/видео")

    # TEST B: micro-batch по 2 видео
    t0 = time.time()
    batch_results = []
    for i in range(0, n, 2):
        batch_results.extend(client.analyze_multi(videos[i:i + 2]))
    batch_time = time.time() - t0
    batch_per_video = batch_time / n
    print(f"B: {n // 2} батчей по 2 видео: {batch_time:.1f}s total, {batch_per_video:.1f}s/видео")

    print()
    print(f"Ускорение на видео: {single_per_video / max(0.1, batch_per_video):.2f}x")
    print(f"Сэкономлено запросов: {n} -> {n // 2} (в 2 раза меньше)")
    print(f"Вердиктов получено: A={len(single_results)}, B={len(batch_results)}")

    # сравнение категорий (согласованность)
    same = sum(
        1 for a, b in zip(single_results, batch_results)
        if a.category == b.category and a.suitable == b.suitable
    )
    print(f"Согласованность категории+suitable: {same}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
