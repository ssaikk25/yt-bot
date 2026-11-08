"""Чистые функции для правил отбора (без сетевых вызовов) — легко тестируются.

Содержит:
- правило 30% (required = ceil(total * ratio), early success/fail);
- candidate score (логарифмический, устойчив к вирусным выбросам);
- query score (нормализованный).
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable


def required_suitable(total: int, ratio: float = 0.30) -> int:
    """Сколько видео должны подойти, чтобы канал прошёл правило ratio."""
    return max(1, math.ceil(total * ratio))


def decide_30_percent(
    passed: int,
    checked: int,
    total: int,
    ratio: float = 0.30,
    min_checked: int = 0,
    border_margin: float = 0.10,
) -> str:
    """Решение по правилу 30%: 'PASS' | 'FAIL' | 'CONTINUE'.

    - PASS: passed >= required И набрана минимальная выборка (min_checked).
    - FAIL: даже если все оставшиеся пройдут, порог недостижим.
    - CONTINUE: продолжать проверку.

    Зачем min_checked: без него правило на знаменателе 1-3 видео срабатывало
    мгновенно (required=max(1,ceil(total*0.3))), давая PASS по одному-двум роликам.

    Зачем border_margin: если наблюдаемая доля близка к порогу (в пределах
    border_margin), добираем доказательства по всему пулу, а не останавливаемся.
    """
    required = required_suitable(total, ratio)
    need = min(min_checked, total) if min_checked and min_checked > 0 else 0

    if passed >= required:
        if checked < need:
            return "CONTINUE"  # мало данных для честного PASS
        observed = passed / max(1, checked)
        if checked < total and observed < ratio + border_margin:
            return "CONTINUE"  # пограничный случай — дорабатываем
        return "PASS"

    remaining = total - checked
    if passed + remaining < required:
        return "FAIL"
    return "CONTINUE"


def median_views(views: Iterable[int]) -> float:
    v = [int(x) for x in views if x >= 0]
    return float(statistics.median(v)) if v else 0.0


def candidate_score(subs: int, views: Iterable[int]) -> float:
    """Нормализованный score канала: чем меньше, тем «меньше» канал (выше приоритет).

    Использует логарифм подписчиков и МЕДИАНЫ просмотров (а не среднего), чтобы один
    вирусный ролик не «раздувал» канал. Формула:
        score = log1p(subscribers) + log1p(median_views)
    """
    return math.log1p(max(0, int(subs))) + math.log1p(median_views(views))


def query_score(
    new_channel_ratio: float,
    low_view_ratio: float,
    low_subscriber_ratio: float = 0.0,
    cheap_filter_pass_ratio: float = 0.0,
    qualified_ratio: float = 0.0,
) -> float:
    """Скор запроса (все входы нормированы в 0..1).

    Формула:
        score = 0.30*new + 0.25*low_view + 0.20*low_subs + 0.15*cheap_pass + 0.10*qualified
    """
    def clip(x: float) -> float:
        return max(0.0, min(1.0, float(x)))
    return (
        0.30 * clip(new_channel_ratio)
        + 0.25 * clip(low_view_ratio)
        + 0.20 * clip(low_subscriber_ratio)
        + 0.15 * clip(cheap_filter_pass_ratio)
        + 0.10 * clip(qualified_ratio)
    )
