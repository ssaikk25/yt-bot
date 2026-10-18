# -*- coding: utf-8 -*-
"""Benchmark эффективности MiMo (сколько вызовов уходит на канал, old vs new).

НЕ вызывает MiMo и сеть. Две части:
1) farm fingerprint на реальных каналах из лога (показывает, что дешёвый сигнал
   отделяет AI-фермы от живых каналов БЕЗ MiMo);
2) симуляция правила 30% (early stop) — точное число MiMo-вызовов до/после
   изменений (порядок + дешёвый отсев «очевидного мусора»).

Запуск: python -m app.eval_benchmark
"""
from __future__ import annotations

import sys

from .precheck import compute_farm_risk, farm_risk_band, title_risk
from .rules import decide_30_percent, required_suitable


# --------------------------------------------------------------------------- #
# 1) Реальные каналы (заголовки/длительность из лога bot.log)
# --------------------------------------------------------------------------- #
def _farm_cases() -> list[tuple[str, list[str], list[float], list[str]]]:
    """(название, titles, durations, published)."""
    # AI-ферма с шаблонными заголовками (uniform duration, регулярные загрузки)
    templated = (
        "TEMPLATED AI-farm (синтетика)",
        [f"AI Story Folge {i}" for i in range(15)],
        [480.0] * 15,
        ["2025-01-01T00:00:00Z"] * 15,
    )
    # Реальная clickbait AI-ферма из лога (Geschichten vom Land)
    clickbait = (
        "Geschichten vom Land (AI-farm, DE)",
        ["Sie kippten 50 Tonnen faule Tomaten auf sein Feld",
         "Sie lachten über den Jungen – bis jeder Klempner der Stadt",
         "Der Junge bat um ihre kaputten Hydraulikpumpen",
         "Alle tauschten ihre alten Bohrmaschinen ein",
         "Alter Maschinist ließ Nachbarn die Hobelmaschine nutzen",
         "Die Werkstatt warf 12 Jahre Metallreste vor ihr Tor",
         "Der Werklehrer ließ sie an die Drehbank",
         "Sie lachten, als der Junge ihre defekten Anlasser nahm"],
        [480.0, 481.0, 479.0, 482.0, 480.0, 478.0, 481.0, 480.0],
        ["2025-01-01T00:00:00Z", "2025-01-01T06:00:00Z", "2025-01-01T12:00:00Z",
         "2025-01-01T18:00:00Z", "2025-01-02T00:00:00Z", "2025-01-02T06:00:00Z",
         "2025-01-02T12:00:00Z", "2025-01-02T18:00:00Z"],
    )
    # Живой маленький канал (Karol Pruzinsky — пчеловодство, разная длительность)
    real = (
        "Karol Pruzinsky (живой, SK)",
        ["Template for grid to beehive", "Collecting nectar and pollen by BEES",
         "Tip: How make frames to BeeHive for Beekepers", "Kontrola stavebného rámika",
         "Získavanie vosku z voštín", "chytanie roja", "Uvitacie video ZO SZV Cadca"],
        [240, 120, 320, 180, 540, 90, 210],
        ["2025-01-01T00:00:00Z", "2025-01-05T12:00:00Z", "2025-02-01T00:00:00Z",
         "2025-03-02T00:00:00Z", "2025-04-10T00:00:00Z", "2025-05-20T00:00:00Z",
         "2025-07-01T00:00:00Z"],
    )
    return [templated, clickbait, real]


def run_farm_fingerprint() -> None:
    print("=== 1) FARM FINGERPRINT (без MiMo) ===")
    print(f"{'канал':44s} {'risk':>5s} {'band':>10s} {'title_risk_max':>14s}")
    for name, titles, durations, published in _farm_cases():
        risk = compute_farm_risk(titles, [""] * len(titles), durations, published)
        tr = max((title_risk(t)[0] for t in titles), default=0.0)
        print(f"{name:44s} {risk:5.2f} {farm_risk_band(risk):>10s} {tr:14.2f}")


# --------------------------------------------------------------------------- #
# 2) Симуляция MiMo-вызовов (правило 30%, early stop)
# --------------------------------------------------------------------------- #
def simulate(labels: list[bool], ratio: float = 0.30) -> tuple[int, str]:
    """Возвращает (число проверенных видео до решения, 'PASS'|'FAIL')."""
    total = len(labels)
    checked = 0
    passed = 0
    for lab in labels:
        checked += 1
        if lab:
            passed += 1
        d = decide_30_percent(passed, checked, total, ratio)
        if d in ("PASS", "FAIL"):
            return checked, d
    return checked, "PASS" if passed >= required_suitable(total, ratio) else "FAIL"


def run_mimo_simulation() -> None:
    print("\n=== 2) MIMO CALLS (симуляция правила 30%) ===")

    print("\n-- Сценарий A: все 15 видео плохие (чистая AI-ферма) --")
    total, req = 15, required_suitable(15)
    print(f"  total={total} required={req} (0 подходящих)")
    # порядок не важен: подходящих нет
    c, d = simulate([False] * total)
    print(f"  MiMo calls = {c} -> {d}  (порядок НЕ меняет: нужен FAIL-proof = total-required+1)")

    print("\n-- Сценарий B: 6 из 15 подходят (живой канал) --")
    labels_scattered = [False, True, False, False, True, False, True, False,
                        False, True, False, True, False, False, True]
    c_old, d_old = simulate(labels_scattered)
    # NEW: риск-порядок ставит «чистые» видео первыми (ожидаемо PASS первыми)
    labels_sorted = sorted(labels_scattered, reverse=True)  # True первыми
    c_new, d_new = simulate(labels_sorted)
    print(f"  OLD (порядок как есть): MiMo calls = {c_old} -> {d_old}")
    print(f"  NEW (сначала вероятные PASS): MiMo calls = {c_new} -> {d_new}")
    print(f"  экономия: {c_old - c_new} вызовов")

    print("\n-- Сценарий C: канал-мусор (5 из 15 отсеяны hard-junk до MiMo) --")
    # 5 видео «gameplay/white noise» отсеяны дешёвым фильтром -> eligible=10, все плохие
    total2, req2 = 10, required_suitable(10)
    c_before = simulate([False] * 15)[0]
    c_after = simulate([False] * total2)[0]
    print(f"  BEFORE (15 eligible, 0 pass): MiMo calls = {c_before} (required={required_suitable(15)})")
    print(f"  AFTER  (10 eligible, 0 pass): MiMo calls = {c_after} (required={req2})")
    print(f"  экономия: {c_before - c_after} вызовов")


def main() -> int:
    run_farm_fingerprint()
    run_mimo_simulation()
    return 0


if __name__ == "__main__":
    sys.exit(main())
