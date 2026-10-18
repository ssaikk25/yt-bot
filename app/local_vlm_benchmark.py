"""Benchmark локального VLM pre-filter (SmolVLM2 / llama.cpp).

Использует УЖЕ извлечённые кадры из `temp/` (никаких лишних обращений к YouTube,
никакого скачивания видео).

Режимы:
  1) shadow-benchmark (по умолчанию):
     python -m app.local_vlm_benchmark --limit 20
     Прогоняет локальную модель И MiMo на ОДНИХ И ТЕХ ЖЕ кадрах и сравнивает.
  2) только локальная модель (без траты токенов MiMo):
     python -m app.local_vlm_benchmark --only-local --limit 20
  3) отчёт по уже накопленному JSONL (без запуска моделей):
     python -m app.local_vlm_benchmark --report

Главный критерий успеха — НЕ «много отказов», а сколько MiMo-вызовов можно
безопасно убрать без риска потерять подходящие видео (смотрим false-reject rate).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .config import load_settings
from .local_vlm import LocalVlmClient
from .logger import get_logger, setup_logging
from .mimo_client import MiMoClient

log = get_logger("local_vlm_bench")

# Сколько кадров брать на видео (QUICK: 10%/50%/90%).
FRAMES_PER_VIDEO = 3
FRAME_LABELS = ["10", "50", "90"]


def _discover_video_dirs(temp_dir: Path) -> list[Path]:
    """Подкаталоги temp/, содержащие кадры (frame_00_*.jpg) — по одному на видео."""
    out: list[Path] = []
    if not temp_dir.exists():
        return out
    for d in sorted(temp_dir.iterdir()):
        if not d.is_dir():
            continue
        # Пропускаем вспомогательные каталоги: DEEP-кадры (<id>_deep) и статичность (static_<id>),
        # чтобы не считать одно видео дважды.
        name = d.name
        if name.endswith("_deep") or name.startswith("static_"):
            continue
        if any(d.glob("frame_00_*.jpg")):
            out.append(d)
    return out


def _load_frames(video_dir: Path, limit: int = FRAMES_PER_VIDEO) -> list[tuple[str, bytes]]:
    """Собирает до `limit` кадров из каталога, сортируя по индексу frame_XX."""
    items = []
    for p in video_dir.glob("frame_*.jpg"):
        name = p.name  # frame_02_290s.jpg
        idx_part = name.split("_", 2)[1] if "_" in name else "99"
        try:
            idx = int(idx_part)
        except ValueError:
            idx = 99
        items.append((idx, p))
    items.sort(key=lambda x: x[0])
    frames = []
    for i, (_, p) in enumerate(items[:limit]):
        try:
            frames.append((FRAME_LABELS[i] if i < len(FRAME_LABELS) else f"{i}", p.read_bytes()))
        except OSError:
            continue
    return frames


def _measure_resources() -> dict:
    """Best-effort замер VRAM (nvidia-smi) и RAM (psutil, если есть)."""
    out: dict = {"gpu": {}, "ram": {}}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            used, total = [int(x) for x in r.stdout.strip().split(",")]
            out["gpu"] = {"used_mb": used, "total_mb": total}
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            procs = []
            for line in r.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 2 and parts[0].isdigit():
                    procs.append({"pid": int(parts[0]), "vram_mb": int(parts[1])})
            out["gpu"]["processes"] = procs
    except Exception:
        pass
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["ram"] = {"used_mb": int(vm.used // (1024 * 1024)), "total_mb": int(vm.total // (1024 * 1024))}
    except Exception:
        out["ram"] = {"note": "psutil не установлен (pip install psutil для замера RAM)"}
    return out


def _print_report(lines: list[dict], summary: dict) -> None:
    n = len(lines) or (summary or {}).get("calls", 0)
    print("=" * 70)
    print(f"Локальный VLM benchmark — отчёт по {n} видео")
    print("=" * 70)
    s = summary or {}
    if s.get("calls"):
        print(f"Вызовов локальной модели : {s.get('calls')}")
        print(f"Ошибок локальной модели  : {s.get('errors')}")
        print(f"Вердикты: REJECT={s.get('verdict_reject')} "
              f"UNCERTAIN={s.get('verdict_uncertain')} "
              f"NO_REJECT={s.get('verdict_no_reject')}")
        print(f"Среднее время локального анализа: {s.get('avg_latency_sec', 0):.2f} c")

    # false-reject и экономия: из summary, если есть; иначе пересчитываем из JSONL.
    false = s.get("false_rejects", 0)
    matched = s.get("reject_matched", 0)
    skipped = s.get("mimo_skipped", 0)
    if not s.get("calls") and lines:
        for line in lines:
            loc = line.get("local", {})
            mimo = line.get("mimo", {})
            if loc.get("verdict") == "OBVIOUS_REJECT" and not loc.get("error"):
                if mimo.get("skipped"):
                    skipped += 1
                elif mimo.get("error"):
                    pass
                elif mimo.get("suitable") is True:
                    false += 1
                else:
                    matched += 1
    reject_total = false + matched
    print(f"Локальных OBVIOUS_REJECT : {reject_total} (сравнено с MiMo) + {skipped} пропущено в active")
    if reject_total:
        frr = false / reject_total
        print(f"FALSE-REJECT rate       : {false}/{reject_total} = {frr:.1%}  (локально REJECT, но MiMo подошёл)")
        print(f"Потенциальная экономия  : до {matched} MiMo-вызовов (REJECT подтверждён MiMo)")
    else:
        print("Пока нет ни одного OBVIOUS_REJECT с ground-truth MiMo — выводов делать рано.")

    flags = s.get("flags", {})
    if flags:
        print("\nПризнаки (precision = доля локальных reject, подтверждённых MiMo):")
        print(f"{'flag':16s} {'rejects':>7s} {'matched':>7s} {'false':>7s} {'precision':>9s}")
        for flag, f in sorted(flags.items(), key=lambda kv: -(kv[1].get("matched", 0))):
            p = f.get("precision")
            p_s = f"{p:.1%}" if isinstance(p, float) else "-"
            print(f"{flag:16s} {f.get('rejects', 0):>7d} {f.get('matched', 0):>7d} "
                  f"{f.get('false', 0):>7d} {p_s:>9s}")


def _run_shadow(settings, limit: int, only_local: bool) -> None:
    temp_dir = Path(settings.temp_dir)
    dirs = _discover_video_dirs(temp_dir)
    if not dirs:
        print("В temp/ нет кадров. Сначала запустите bot, чтобы извлечь кадры.")
        return
    dirs = dirs[:limit]
    print(f"Видео для benchmark: {len(dirs)} (кадры уже в temp/)")

    client = LocalVlmClient.from_settings(settings)
    mimo = MiMoClient(
        api_key=settings.mimo_api_key, base_url=settings.mimo_base_url,
        model=settings.mimo_model, categories=settings.categories,
        timeout=settings.mimo_timeout, retries=settings.mimo_retries,
        max_tokens=settings.mimo_max_tokens, temperature=settings.mimo_temperature,
        use_response_format=settings.mimo_use_response_format,
        enable_thinking=settings.mimo_enable_thinking,
    )

    print("\nРесурсы ДО прогона:")
    print(json.dumps(_measure_resources(), ensure_ascii=False, indent=2))

    local_times: list[float] = []
    for d in dirs:
        frames = _load_frames(d)
        if len(frames) < 2:
            continue
        vid = d.name
        meta = {"video_id": vid, "title": ""}
        t0 = time.time()
        local = client.analyze_frames(frames, meta, target_category=None)
        local_t = time.time() - t0
        local_times.append(local_t)

        if only_local:
            # sanity-check: не пишем в comparison-трекер, только меряем модель
            print(f"{vid}: local={local.verdict} flags={','.join(local.flags) or '-'} ({local_t:.2f}s)")
            continue

        try:
            t0 = time.time()
            analysis = mimo.analyze([b for _, b in frames])
            mimo_t = time.time() - t0
            mimo_suitable = analysis.suitable
            mimo_flags = analysis.flags
            mimo_category = analysis.category
            mimo_conf = analysis.confidence
            mimo_error = False
        except Exception as e:
            log.warning("MiMo %s: %s", vid, str(e)[:120])
            mimo_suitable, mimo_flags, mimo_category, mimo_conf, mimo_error = None, {}, "", 0.0, True
            mimo_t = 0.0

        client.tracker.record(local, mimo_suitable=mimo_suitable, mimo_flags=mimo_flags,
                              mimo_category=mimo_category, mimo_confidence=mimo_conf,
                              mimo_error=mimo_error,
                              context={"video_id": vid})
        print(f"{vid}: local={local.verdict} [{','.join(local.flags) or '-'}] "
              f"({local_t:.2f}s) | MiMo suitable={mimo_suitable} ({mimo_t:.2f}s)")

    print("\nРесурсы ПОСЛЕ прогона:")
    print(json.dumps(_measure_resources(), ensure_ascii=False, indent=2))
    if only_local:
        if local_times:
            print(f"\nСреднее время локального анализа: {sum(local_times)/len(local_times):.2f} c "
                  f"(n={len(local_times)})")
        return
    _print_report(lines=[], summary=client.tracker.summary())


def _report_only(settings) -> None:
    jsonl = Path(settings.logs_dir) / "local_vlm_compare.jsonl"
    summary_path = Path(settings.logs_dir) / "local_vlm_summary.json"
    lines = []
    if jsonl.exists():
        try:
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if raw:
                    lines.append(json.loads(raw))
        except Exception as e:
            print(f"Ошибка чтения JSONL: {e}")
    summary = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    _print_report(lines, summary)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="сколько видео (кадров) прогнать")
    ap.add_argument("--only-local", action="store_true", help="только локальная модель, без MiMo")
    ap.add_argument("--report", action="store_true", help="отчёт по накопленному JSONL без запуска моделей")
    args = ap.parse_args()

    settings = load_settings()
    setup_logging(Path(settings.logs_dir))

    if args.report:
        _report_only(settings)
        return 0
    # Для benchmark всегда прогоняем сравнение (shadow): даже если LOCAL_VLM_ENABLED=false
    # или mode=active, здесь мы хотим измерить локальную модель и сравнить с MiMo.
    settings.local_vlm_enabled = True
    settings.local_vlm_mode = "shadow"
    _run_shadow(settings, args.limit, args.only_local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
