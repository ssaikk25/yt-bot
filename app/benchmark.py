"""Benchmark discovery: сравнение конфигураций (ytsearchN × workers).

Запуск:  python -m app.benchmark

Сравнивает конфигурации и показывает главный KPI — новых уникальных маленьких
каналов в час (а не число найденных роликов).
"""
from __future__ import annotations

import statistics
import sys
import time
from dataclasses import dataclass

from .config import load_settings
from .discovery import DiscoveryEngine
from .logger import get_logger, setup_logging
from .youtube_client import YouTubeClient

log = get_logger("benchmark")

# Конфигурации по умолчанию: (results_per_query, workers)
DEFAULT_CONFIGS = [
    (30, 1),   # A: baseline ytsearch30, 1 worker
    (50, 1),   # B: ytsearch50, 1 worker
    (50, 4),   # C: ytsearch50, 4 workers
    (75, 4),   # D: ytsearch75, 4 workers
    (50, 6),   # E: ytsearch50, 6 workers
]


@dataclass
class BenchResult:
    label: str
    results: int
    workers: int
    total_results: int = 0
    unique_channels: int = 0
    below_1k: int = 0
    below_3k: int = 0
    below_10k: int = 0
    query_times: list[float] = None
    total_time: float = 0.0
    errors: int = 0

    def __post_init__(self):
        if self.query_times is None:
            self.query_times = []

    @property
    def avg_query_time(self) -> float:
        return statistics.mean(self.query_times) if self.query_times else 0.0

    @property
    def median_query_time(self) -> float:
        return statistics.median(self.query_times) if self.query_times else 0.0

    @property
    def channels_per_hour(self) -> float:
        if self.total_time <= 0:
            return 0.0
        return self.unique_channels / self.total_time * 3600.0


def _run_config(settings, youtube, results, workers) -> BenchResult:
    s = settings
    s.results_per_query = results
    s.discovery_workers = workers
    engine = DiscoveryEngine(s, youtube)
    t0 = time.time()
    channels = engine.run(seen_channels=set(), save_seen=False)
    total_time = time.time() - t0

    r = BenchResult(label=f"ytsearch{results} x{workers}", results=results, workers=workers)
    r.total_time = total_time
    r.unique_channels = len(channels)
    for st in engine.stats.values():
        r.total_results += st.result_count
        r.errors += st.errors
        if st.time_sec > 0:
            r.query_times.append(st.time_sec)
    for ch in channels:
        subs = ch.subscriber_count
        if subs < 1000:
            r.below_1k += 1
        if subs < 3000:
            r.below_3k += 1
        if subs < 10000:
            r.below_10k += 1
    return r


def run_benchmark(configs=None) -> list[BenchResult]:
    settings = load_settings()
    setup_logging(settings)
    if not settings.youtube_api_key:
        log.error("Нет YOUTUBE_API_KEY")
        return []
    youtube = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
    cfgs = configs or DEFAULT_CONFIGS

    out: list[BenchResult] = []
    for results, workers in cfgs:
        log.info("=== Benchmark %s (ytsearch%d, %d workers) ===", f"{results}x{workers}", results, workers)
        try:
            r = _run_config(settings, youtube, results, workers)
        except Exception as e:
            log.exception("Benchmark %s: %s", f"{results}x{workers}", e)
            r = BenchResult(label=f"ytsearch{results} x{workers} (ERROR)", results=results, workers=workers)
            r.errors += 1
        out.append(r)
        log.info("  unique=%d  below1k=%d  below3k=%d  time=%.1fs  ch/h=%.0f",
                 r.unique_channels, r.below_1k, r.below_3k, r.total_time, r.channels_per_hour)
    return out


def _print_table(results: list[BenchResult]) -> None:
    print("\n" + "=" * 100)
    print("DISCOVERY BENCHMARK")
    print("=" * 100)
    hdr = ("config", "unique_ch", "<1k", "<3k", "<10k", "results", "avg_q(s)", "med_q(s)", "total(s)", "err", "ch/hour")
    print(f"{hdr[0]:22s} {hdr[1]:>9s} {hdr[2]:>6s} {hdr[3]:>6s} {hdr[4]:>6s} {hdr[5]:>8s} {hdr[6]:>8s} {hdr[7]:>8s} {hdr[8]:>8s} {hdr[9]:>4s} {hdr[10]:>8s}")
    for r in results:
        print(f"{r.label:22s} {r.unique_channels:9d} {r.below_1k:6d} {r.below_3k:6d} {r.below_10k:6d} "
              f"{r.total_results:8d} {r.avg_query_time:8.2f} {r.median_query_time:8.2f} {r.total_time:8.1f} "
              f"{r.errors:4d} {r.channels_per_hour:8.0f}")
    print("=" * 100)
    if results:
        best = max(results, key=lambda r: r.channels_per_hour)
        print(f"BEST by new channels/hour: {best.label} -> {best.channels_per_hour:.0f} ch/hour")
    print("=" * 100)


def main() -> int:
    results = run_benchmark()
    _print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
