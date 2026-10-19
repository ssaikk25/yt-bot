"""DeepSeek vs MiMo: сравнение на одних и тех же кадрах из temp/."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from .category_mapper import canonicalize
from .config import load_settings
from .deepseek_client import DeepSeekClient
from .logger import get_logger, setup_logging
from .mimo_client import MiMoClient

log = get_logger("deepseek_bench")


def _dirs(temp_dir: Path) -> list[Path]:
    out = []
    if not temp_dir.exists():
        return out
    for d in sorted(temp_dir.iterdir()):
        if d.is_dir() and not d.name.endswith("_deep") and not d.name.startswith("static_") \
                and any(d.glob("frame_00_*.jpg")):
            out.append(d)
    return out


def _frames(d: Path, n: int = 3) -> list[bytes]:
    out = []
    for i, p in enumerate(sorted(d.glob("frame_0[0-2]_*.jpg"))[:n]):
        try:
            out.append(p.read_bytes())
        except OSError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    s = load_settings()
    setup_logging(Path(s.logs_dir))
    if not s.deepseek_api_key:
        print("DEEPSEEK_API_KEY не задан")
        return 2

    ds = DeepSeekClient(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url,
                        model=s.deepseek_model, max_tokens=s.deepseek_max_tokens,
                        timeout=s.deepseek_timeout)
    mimo = MiMoClient(api_key=s.mimo_api_key, base_url=s.mimo_base_url, model=s.mimo_model,
                      categories=s.categories, timeout=s.mimo_timeout, retries=s.mimo_retries,
                      max_tokens=s.mimo_max_tokens, temperature=s.mimo_temperature,
                      use_response_format=s.mimo_use_response_format,
                      enable_thinking=s.mimo_enable_thinking)

    dirs = _dirs(Path(s.temp_dir))[:args.limit]
    print(f"Видео: {len(dirs)} | DeepSeek: {s.deepseek_model} | MiMo: {s.mimo_model}\n")

    rows, ds_t, mimo_t = [], [], []
    for d in dirs:
        imgs = _frames(d)
        if len(imgs) < 2:
            continue
        vid = d.name
        t0 = time.time()
        try:
            a = ds.analyze(imgs); a_ok = True
        except Exception as e:
            a, a_ok = None, False
            log.warning("DeepSeek %s: %s", vid, str(e)[:120])
        ds_t.append(time.time() - t0)

        t0 = time.time()
        try:
            m = mimo.analyze(imgs); m_ok = True
        except Exception as e:
            m, m_ok = None, False
            log.warning("MiMo %s: %s", vid, str(e)[:120])
        mimo_t.append(time.time() - t0)

        if a_ok and m_ok:
            rows.append({
                "video_id": vid,
                "ds": {"suitable": a.suitable, "cat": canonicalize(a.category) or a.category},
                "mimo": {"suitable": m.suitable, "cat": canonicalize(m.category) or m.category},
                "agree_suitable": a.suitable == m.suitable,
                "agree_category": bool(canonicalize(a.category) and canonicalize(a.category) == canonicalize(m.category)),
            })
        print(f"{vid}: DeepSeek={a.suitable if a_ok else 'ERR'} ({ds_t[-1]:.1f}s) | "
              f"MiMo={m.suitable if m_ok else 'ERR'} ({mimo_t[-1]:.1f}s)")

    n = len(rows)
    print("\n" + "=" * 60)
    print(f"Отчёт DeepSeek vs MiMo (n={n})")
    print("=" * 60)
    if n:
        agree = sum(1 for r in rows if r["agree_suitable"])
        fn = sum(1 for r in rows if not r["ds"]["suitable"] and r["mimo"]["suitable"])
        fp = sum(1 for r in rows if r["ds"]["suitable"] and not r["mimo"]["suitable"])
        cat = sum(1 for r in rows if r["agree_category"])
        print(f"Согласие suitable: {agree}/{n} = {agree/n:.0%}")
        print(f"  ложный отказ (DeepSeek нет, MiMo да): {fn}")
        print(f"  пропущенный брак (DeepSeek да, MiMo нет): {fp}")
        print(f"Согласие категории: {cat}/{n} = {cat/n:.0%}")
    print(f"Среднее время DeepSeek: {statistics.mean(ds_t):.1f} c")
    print(f"Среднее время MiMo: {statistics.mean(mimo_t):.1f} c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
