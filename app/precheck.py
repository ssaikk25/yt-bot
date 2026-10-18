# -*- coding: utf-8 -*-
"""Дешёвый (без AI) анализ каналов и видео ПЕРЕД MiMo.

Содержит только Python: нормализация текста, шаблоны заголовков, Jaccard-сходство,
регулярность длительности/загрузок, риск-скор заголовка, fingerprint канала
(risk score) и умный порядок проверки видео.

НЕ использует MiMo, НЕ меняет правило 30%, НЕ делает автоматический reject по
мягким признакам — только scoring / ordering / logging.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from datetime import datetime
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Жёсткий «очевидный мусор» (уже существующая политика проекта в discovery,
# здесь — token-точное сопоставление). Применяется к пулу видео как дешёвый
# технический отсев ДО MiMo, чтобы не тратить MiMo на заведомый мусор.
# --------------------------------------------------------------------------- #
HARD_JUNK_KEYWORDS = [
    "simulator", "gameplay", "gaming", "walkthrough", "minecraft", "roblox",
    "fortnite", "game simulator", "car game", "robot game", "3d games",
    "симулятор", "геймплей", "прохождение", "simulador", "juego de",
    "white noise", "ruido blanco", "sonido de", "sleep music", "dormir",
    "relajarse", "calmar la mente", "relaxing music", "ambient music",
    "для сна", "релакс", "медитация", "meditation", "asmr", "lofi", "sleep sounds",
]

# --------------------------------------------------------------------------- #
# Мягкие маркеры: повышают риск/приоритет, но НЕ дают автоматический reject.
# --------------------------------------------------------------------------- #
SOFT_KEYWORD_GROUPS: dict[str, float] = {
    "ai_content": 1.0,       # явный признак AI-генерации
    "gaming": 0.9,
    "entertainment": 0.8,    # top10/compilation/reaction
    "relax": 0.7,
    "talking_head": 0.6,     # podcast/interview/vlog
    "news_trailer": 0.5,
}
SOFT_KEYWORDS: dict[str, list[str]] = {
    "ai_content": ["ai story", "ai generated", "ai-generated", "generated story",
                   "made with ai", "ai short", "ai film", "ai animation"],
    "gaming": ["gameplay", "gaming", "walkthrough", "simulator", "minecraft",
               "roblox", "fortnite", "speedrun", "lets play"],
    "entertainment": ["top 10", "top 5", "top 15", "top 20", "top 100",
                      "compilation", "best moments", "reaction", "try not to laugh",
                      "highlights", "best of", "funny moments"],
    "relax": ["white noise", "sleep music", "sleep sounds", "relaxing",
              "meditation", "asmr", "ambient music", "lofi"],
    "talking_head": ["podcast", "interview", "q&a", "vlog", "storytime",
                     "reacts to", "reaction video"],
    "news_trailer": ["breaking news", "trailer", "teaser", "official music video",
                     "music video"],
}


# --------------------------------------------------------------------------- #
# Текст
# --------------------------------------------------------------------------- #
def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", (s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def tokenize(s: str) -> list[str]:
    return re.findall(r"[0-9a-zа-яёіїєґ\u00c0-\u024f]+", normalize_text(s))


def _subsequence(needle: list[str], haystack: list[str]) -> bool:
    k = len(needle)
    if k == 0:
        return False
    for i in range(len(haystack) - k + 1):
        if haystack[i:i + k] == needle:
            return True
    return False


def _keyword_tokens(kw: str) -> list[str]:
    return tokenize(kw)


def title_to_template(title: str) -> str:
    """'Geschichten vom Land – Folge 183' -> 'geschichten vom land folge #'."""
    t = re.sub(r"[0-9]+", "#", normalize_text(title))
    toks = re.findall(r"[#]|[0-9a-zа-яёіїєґ\u00c0-\u024f]+", t)
    return " ".join(toks)


def dominant_template_ratio(titles: Iterable[str]) -> tuple[int, float, str]:
    """(count_доминирующего шаблона, его доля, сам шаблон)."""
    counts: dict[str, int] = {}
    for t in titles:
        tmpl = title_to_template(t)
        counts[tmpl] = counts.get(tmpl, 0) + 1
    if not counts:
        return 0, 0.0, ""
    dominant = max(counts, key=counts.get)
    n = sum(counts.values())
    return counts[dominant], counts[dominant] / n, dominant


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mean_pairwise_jaccard(strings: Iterable[str]) -> float:
    sets = [set(tokenize(s)) for s in strings if s]
    if len(sets) < 2:
        return 0.0
    sims = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            sims.append(jaccard(sets[i], sets[j]))
    return statistics.mean(sims) if sims else 0.0


# --------------------------------------------------------------------------- #
# Регулярность
# --------------------------------------------------------------------------- #
def duration_regularity(durations: Iterable[float]) -> tuple[float, float, float, float]:
    """(mean, median, population_std, coefficient_of_variation)."""
    d = [float(x) for x in durations if x and x > 0]
    if not d:
        return 0.0, 0.0, 0.0, 0.0
    mean = statistics.mean(d)
    if len(d) < 2:
        return mean, mean, 0.0, 0.0
    std = statistics.pstdev(d)
    cv = std / mean if mean > 0 else 0.0
    return mean, statistics.median(d), std, cv


def upload_regularity(published_at: Iterable[str]) -> tuple[float, float]:
    """(cv интервалов загрузки, farm_score) — низкий cv = очень регулярно."""
    times = []
    for p in published_at:
        try:
            times.append(datetime.fromisoformat((p or "").replace("Z", "+00:00")))
        except Exception:
            continue
    times.sort()
    if len(times) < 3:
        return 0.0, 0.0
    intervals = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    mean = statistics.mean(intervals)
    if mean <= 0:
        return 0.0, 0.0
    cv = statistics.pstdev(intervals) / mean
    return cv, max(0.0, 1.0 - min(1.0, cv))


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
def title_risk(title: str) -> tuple[float, dict[str, str]]:
    """Мягкий риск заголовка (0..1) + какие группы сработали."""
    toks = tokenize(title)
    score = 0.0
    hits: dict[str, str] = {}
    for group, keywords in SOFT_KEYWORDS.items():
        for kw in keywords:
            if _subsequence(_keyword_tokens(kw), toks):
                g = SOFT_KEYWORD_GROUPS[group]
                if g > score:
                    score = g
                hits[group] = kw
                break
    return score, hits


def is_hard_junk_title(title: str) -> bool:
    """Жёсткий «очевидный мусор» (token-точное совпадение, без ложных по подстроке)."""
    toks = tokenize(title)
    for kw in HARD_JUNK_KEYWORDS:
        if _subsequence(_keyword_tokens(kw), toks):
            return True
    return False


def compute_farm_risk(
    titles: list[str],
    descriptions: list[str],
    durations: list[float],
    published_at: list[str],
    w_title: float = 0.35,
    w_desc: float = 0.20,
    w_duration: float = 0.15,
    w_upload: float = 0.10,
    w_thumbnail: float = 0.20,
    thumbnail_similarity: Optional[float] = None,
) -> float:
    """Channel-level AI-farm risk (0..1). Чисто дешёвые признаки, без AI.

    title-сходство = max(средний Jaccard, доля доминирующего шаблона).
    Нормализация идёт по ВЕСАМ НАЛИЧНЫХ сигналов: отсутствующие данные
    (нет описаний/миниатюр/длительностей) не размывают оценку.
    """
    tpl_count, tpl_ratio, _ = dominant_template_ratio(titles)
    title_sim = max(mean_pairwise_jaccard(titles), tpl_ratio)
    has_desc = any((d or "").strip() for d in descriptions)
    desc_sim = mean_pairwise_jaccard(descriptions)
    _, _, _, cv_dur = duration_regularity(durations)
    dur_score = max(0.0, 1.0 - min(1.0, cv_dur))
    has_dur = any(float(x) > 0 for x in durations if x)
    cv_up, up_score = upload_regularity(published_at)
    # «есть данные» по загрузкам = есть >=3 валидных даты
    has_upload = _valid_published_count(published_at) >= 3

    pairs: list[tuple[float, float]] = [(w_title, title_sim)]
    if has_desc:
        pairs.append((w_desc, desc_sim))
    if has_dur:
        pairs.append((w_duration, dur_score))
    if has_upload:
        pairs.append((w_upload, up_score))
    if thumbnail_similarity is not None:
        pairs.append((w_thumbnail, thumbnail_similarity))

    total_w = sum(w for w, _ in pairs)
    if total_w <= 0:
        return 0.0
    score = sum(w * s for w, s in pairs) / total_w
    return max(0.0, min(1.0, score))


def _valid_published_count(published_at: Iterable[str]) -> int:
    n = 0
    for p in published_at:
        try:
            datetime.fromisoformat((p or "").replace("Z", "+00:00"))
            n += 1
        except Exception:
            continue
    return n


def farm_risk_band(risk: float, high: float = 0.6, very_high: float = 0.8) -> str:
    if risk >= very_high:
        return "VERY_HIGH"
    if risk >= high:
        return "HIGH"
    if risk >= 0.3:
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------- #
# Умный порядок проверки видео
# --------------------------------------------------------------------------- #
def video_priority(video_title: str, farm_risk: float,
                   w_title: float = 0.4, w_farm: float = 0.3,
                   w_tech: float = 0.2, w_info: float = 0.1) -> float:
    """precheck_priority (0..1) — для логирования и отладки scheduler'а."""
    tr, _ = title_risk(video_title)
    tech = 0.5  # техническая уверенность по умолчанию (без probe)
    info = 0.5  # информационный выигрыш по умолчанию (уточняется контекстом)
    return min(1.0, w_title * tr + w_farm * farm_risk + w_tech * tech + w_info * info)


def order_videos(videos, farm_risk: float, threshold: float = 0.5):
    """Сортирует видео: для ферм — сначала самые подозрительные (быстрый confirm),
    для нормальных каналов — сначала наименее подозрительные (быстрый PASS)."""
    scored = [(v, title_risk(getattr(v, "title", ""))[0]) for v in videos]
    if farm_risk >= threshold:
        scored.sort(key=lambda x: -x[1])   # рискованные первыми
    else:
        scored.sort(key=lambda x: x[1])    # «чистые» первыми
    return [v for v, _ in scored]
