"""Поиск каналов через yt-dlp (ytsearch) — параллельный, со скорингом запросов.

НЕ тратит квоту YouTube Data API search (используется внутренний поиск YouTube).

Улучшения относительно старой версии:
- параллельный discovery (asyncio + semaphore, adaptive concurrency);
- ytsearchN с настраиваемым N (RESULTS_PER_QUERY);
- статистика и скоринг запросов (novelty + low-view + cheap-filter pass);
- нормализованный candidate score (логарифмический, устойчивый к выбросам);
- дедупликация по channel_id;
- персистентный список уже виденных каналов (JSON, не БД) для novelty.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from .expansion_pool import EXPANSION_POOL, EXPERIMENT_POOL
from .expansion_pool_950 import EXPANSION_950
from .logger import get_logger
from .models import ChannelCandidate
from .query_pool import QueryPool, QUERY_POOL, load_pool_from_yaml, normalize_query

log = get_logger("discovery")

# Названия видео, явно указывающие на игровой контент или «белый шум»/релакс-лупы.
SKIP_TITLE_KEYWORDS = [
    "simulator", "gameplay", "gaming", "walkthrough", "minecraft", "roblox",
    "fortnite", "game simulator", "car game", "robot game", "3d games",
    "симулятор", "геймплей", "прохождение", "simulador", "juego de",
    "white noise", "ruido blanco", "sonido de", "sleep music", "dormir",
    "relajarse", "calmar la mente", "relaxing music", "ambient music",
    "для сна", "релакс", "медитация", "meditation",
]

BOT_BLOCK_MARKERS = ("sign in to confirm you", "not a bot", "bot-block")

# Бонус приоритета для ещё ни разу не прогонявшихся запросов (runs=0).
# Позволяет быстрее покрыть все ~1455 запросов пула, не отключая адаптацию.
UNTRIED_QUERY_BONUS = 1.5

# Категории, которые НЕ ищем и НЕ берём (например, аниме).
EXCLUDED_QUERY_CATEGORIES = {"Аниме/Анимации"}


def _title_skipped(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in SKIP_TITLE_KEYWORDS)


def _yt_search(query: str, n: int, cookies_file: str = "") -> list[dict]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": True,
        "socket_timeout": 30,  # против зависания на сетевых операциях
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return info.get("entries") or []


@dataclass
class QueryStat:
    norm: str = ""
    query: str = ""
    language: str = ""
    region: str = ""
    pool: str = ""
    theme: str = ""
    subtheme: str = ""
    result_count: int = 0
    unique_channels: int = 0
    new_channels: int = 0
    low_view_channels: int = 0
    avg_views: float = 0.0
    median_views: float = 0.0
    cheap_filter_pass: int = 0
    time_sec: float = 0.0
    bot_check: int = 0
    errors: int = 0
    novelty: float = 0.0
    score: float = 0.0
    runs: int = 0
    new_ratio: float = 0.0
    priority_ema: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query, "language": self.language, "region": self.region,
            "pool": self.pool, "theme": self.theme, "subtheme": self.subtheme,
            "result_count": self.result_count, "unique_channels": self.unique_channels,
            "new_channels": self.new_channels, "low_view_channels": self.low_view_channels,
            "avg_views": round(self.avg_views, 1), "median_views": round(self.median_views, 1),
            "cheap_filter_pass": self.cheap_filter_pass, "time_sec": round(self.time_sec, 2),
            "bot_check": self.bot_check, "errors": self.errors,
            "novelty": round(self.novelty, 3), "score": round(self.score, 3),
            "runs": self.runs, "new_ratio": round(self.new_ratio, 3),
            "priority_ema": round(self.priority_ema, 3),
        }


def _seen_channels_path(settings) -> Path:
    return Path(settings.logs_dir) / "discovery_seen_channels.json"


def _load_seen_channels(settings) -> set[str]:
    p = _seen_channels_path(settings)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x) for x in data}
    except Exception:
        pass
    return set()


def _save_seen_channels(settings, seen: set[str]) -> None:
    p = _seen_channels_path(settings)
    try:
        p.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _candidate_score(subs: int, views_list: list[int]) -> float:
    """Нормализованный score: меньше = «меньше» канал = выше приоритет.
    Логарифмическая шкала устойчива к выбросам (один вирусный ролик не ломает сортировку)."""
    med = statistics.median(views_list) if views_list else 0
    return math.log1p(max(0, subs)) + math.log1p(max(0, med))


class DiscoveryEngine:
    def __init__(self, settings, youtube):
        self.settings = settings
        self.youtube = youtube
        # BASE (нетронутый) + EXPANSION + EXPERIMENT. BASE тегируется pool="base".
        base = [dict(q, pool="base", subtheme=q.get("category", "")) for q in QUERY_POOL]
        base += [dict(q, pool="base", subtheme=q.get("category", "")) for q in load_pool_from_yaml(settings)]
        all_queries = base + [dict(q) for q in EXPANSION_POOL] + [dict(q) for q in EXPERIMENT_POOL] + [dict(q) for q in EXPANSION_950]
        # Не ищем по исключённым категориям (аниме и т.п.).
        all_queries = [
            q for q in all_queries
            if (q.get("category") or "").strip() not in EXCLUDED_QUERY_CATEGORIES
        ]
        self.pool = QueryPool(all_queries)
        self.stats: dict[str, QueryStat] = {}
        self.priorities: dict[str, dict] = self._load_priorities()
        self.workers = max(1, settings.discovery_workers)
        self.min_workers = 2

    def _load_priorities(self) -> dict[str, dict]:
        """Загружает накопленные runs/EMA из query_priorities.json (persistent)."""
        p = Path(self.settings.logs_dir) / "query_priorities.json"
        out: dict[str, dict] = {}
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                for norm, d in data.items():
                    out[norm] = {
                        "runs": int(d.get("runs", 0) or 0),
                        "ema": float(d.get("ema", 0.5) or 0.5),
                    }
        except Exception:
            pass
        return out

    def _save_priorities(self) -> None:
        """Сохраняет кумулятивные runs/EMA (переживают прогоны и рестарты)."""
        p = Path(self.settings.logs_dir) / "query_priorities.json"
        try:
            p.write_text(json.dumps(self.priorities, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception:
            pass

    def _query_priority(self, q: dict) -> float:
        """Приоритет запроса: EMA new_ratio (если накоплено достаточно прогонов),
        иначе начальный weight. НЕ отключаем после одного плохого прогона."""
        prev = self.priorities.get(q["_norm"], {"runs": 0, "ema": 0.5})
        if prev.get("runs", 0) >= self.settings.min_query_runs_before_adaptation:
            return prev.get("ema", 0.5)
        return float(q.get("weight", 0.5))

    def _is_untried(self, q: dict) -> bool:
        """True, если запрос ещё ни разу не прогонялся (нет записей или runs==0)."""
        prev = self.priorities.get(q["_norm"])
        return prev is None or int(prev.get("runs", 0) or 0) == 0

    def _select_pool(self, pool_queries: list[dict], n: int) -> list[dict]:
        """Стратифицированный по темам выбор до n запросов.

        - Phase 1 (explore): по одному СЛУЧАЙНОМУ запросу из каждой темы —
          гарантирует разброс по темам и случайность внутри темы.
        - Phase 2 (exploit): оставшиеся слоты — по приоритету + бонусу темы,
          так что темы/запросы, дающие больше новых каналов, получают больше
          внимания (adaptive attention).
        """
        if n <= 0 or not pool_queries:
            return []
        n = min(n, len(pool_queries))

        by_theme: dict[str, list[dict]] = {}
        for q in pool_queries:
            theme = q.get("category") or q.get("subtheme") or "?"
            by_theme.setdefault(theme, []).append(q)

        # score темы = средний приоритет её запросов (для adaptive attention)
        theme_score = {
            t: sum(self._query_priority(q) for q in qs) / max(1, len(qs))
            for t, qs in by_theme.items()
        }

        chosen: list[dict] = []
        chosen_norms: set[str] = set()

        # Phase 1: случайный разброс по темам (по одному query из каждой),
        # внутри темы предпочитаем ещё не прогонявшийся запрос (runs=0).
        themes = list(by_theme.keys())
        random.shuffle(themes)
        for t in themes:
            if len(chosen) >= n:
                break
            candidates = by_theme[t]
            untried = [q for q in candidates if self._is_untried(q)]
            bucket = untried if untried else candidates
            q = random.choice(bucket)
            if q["_norm"] not in chosen_norms:
                chosen.append(q)
                chosen_norms.add(q["_norm"])

        # Phase 2: adaptive attention — приоритет + бонус темы + бонус untried + джиттер
        remaining = n - len(chosen)
        if remaining > 0:
            scored: list[tuple[float, dict]] = []
            for q in pool_queries:
                if q["_norm"] in chosen_norms:
                    continue
                t = q.get("category") or q.get("subtheme") or "?"
                p = self._query_priority(q) + 0.5 * theme_score.get(t, 0.5)
                if self._is_untried(q):
                    p += UNTRIED_QUERY_BONUS
                scored.append((p + random.uniform(0, 0.05), q))
            scored.sort(key=lambda x: -x[0])
            for _, q in scored:
                if len(chosen) >= n:
                    break
                if q["_norm"] not in chosen_norms:
                    chosen.append(q)
                    chosen_norms.add(q["_norm"])

        return chosen[:n]

    def _mixed_queries(self) -> list[dict]:
        """Смешивание пулов по конфигурируемым ratio (BASE/EXPANSION/EXPERIMENT)."""
        queries = self.pool.queries
        base = [q for q in queries if q.get("pool") == "base"]
        exp = [q for q in queries if q.get("pool") == "expansion"]
        exr = [q for q in queries if q.get("pool") == "experiment"]
        n = self.settings.discovery_queries_per_run
        n_base = int(round(n * self.settings.base_query_ratio))
        n_exp = int(round(n * self.settings.expansion_query_ratio))
        n_exr = max(0, n - n_base - n_exp)
        chosen = self._select_pool(base, n_base)
        chosen += self._select_pool(exp, n_exp)
        chosen += self._select_pool(exr, n_exr)
        return chosen

    # ------------------------------------------------------------------ #
    async def _search_one(self, q: dict, results: int, sem: asyncio.Semaphore):
        """Возвращает (QueryStat, channel_views), где channel_views: cid -> list[views]."""
        norm = q["_norm"]
        st = QueryStat(norm=norm, query=q.get("query", ""), language=q.get("language", ""),
                       region=q.get("region", "") or "", pool=q.get("pool", ""),
                       theme=q.get("category", "") or "", subtheme=q.get("subtheme", "") or "")
        channel_views: dict[str, list[int]] = {}
        async with sem:
            await asyncio.sleep(0.15)  # мягкий throttle против bot-check
            t0 = time.time()
            try:
                entries = await asyncio.to_thread(
                    _yt_search, q.get("query", ""), results, self.settings.cookies_file
                )
            except Exception as e:
                st.errors = 1
                if any(m in str(e).lower() for m in BOT_BLOCK_MARKERS):
                    st.bot_check = 1
                st.time_sec = time.time() - t0
                log.warning("yt-dlp поиск %r: %s", q.get("query", ""), str(e)[:110])
                return st, channel_views
        st.time_sec = time.time() - t0
        st.result_count = len(entries)

        for e in entries:
            vid = e.get("id")
            cid = e.get("channel_id")
            if not vid or not cid:
                continue
            views = int(e.get("view_count") or 0)
            dur = float(e.get("duration") or 0)
            title = e.get("title") or ""
            if dur and dur < self.settings.discovery_min_duration:
                continue
            if _title_skipped(title):
                continue
            channel_views.setdefault(cid, []).append(views)

        st.unique_channels = len(channel_views)
        if channel_views:
            medians = {c: statistics.median(v) for c, v in channel_views.items()}
            st.low_view_channels = sum(1 for m in medians.values() if m < self.settings.low_view_threshold_1)
            all_views = [v for vv in channel_views.values() for v in vv]
            st.avg_views = statistics.mean(all_views) if all_views else 0.0
            st.median_views = statistics.median(all_views) if all_views else 0.0
        return st, channel_views

    # ------------------------------------------------------------------ #
    def run(self, seen_channels: Optional[set[str]] = None, save_seen: bool = True) -> list[ChannelCandidate]:
        settings = self.settings
        queries = self._mixed_queries()  # BASE/EXPANSION/EXPERIMENT по ratio + adaptive priority
        if not queries:
            log.warning("query pool пуст")
            return []

        sem = asyncio.Semaphore(self.workers)

        async def _run_all():
            return await asyncio.gather(
                *[self._search_one(q, settings.results_per_query, sem) for q in queries]
            )

        results = asyncio.run(_run_all())

        # Adaptive concurrency: при ошибках/bot-check снижаем, при чистом проходе — плавно растим.
        total_bot = sum(1 for st, _ in results if st.bot_check)
        total_err = sum(1 for st, _ in results if st.errors)
        if total_bot or total_err >= max(3, len(results) * 0.1):
            self.workers = max(self.min_workers, self.workers - 1)
            log.info("adaptive concurrency: ошибок=%d bot_check=%d -> workers=%d", total_err, total_bot, self.workers)
        elif self.workers < self.settings.max_discovery_workers and total_err == 0:
            self.workers += 1
            log.info("adaptive concurrency: чистый проход -> workers=%d", self.workers)

        seen = seen_channels if seen_channels is not None else _load_seen_channels(settings)

        # агрегация по каналам (дедупликация по channel_id)
        channel_views: dict[str, list[int]] = {}
        channel_titles: dict[str, str] = {}
        channel_src: dict[str, dict] = {}
        for q, (st, cv) in zip(queries, results):
            self.stats[st.norm] = st
            for cid, views in cv.items():
                channel_views.setdefault(cid, []).extend(views)
                channel_titles.setdefault(cid, "")
                channel_src.setdefault(cid, {
                    "language": q.get("language", ""),
                    "region": q.get("region", ""),
                    "source_query": q.get("query", ""),
                })

        # новизна + score + EMA-priority по запросу (channel_views есть в results)
        for st, cv in results:
            new_cnt = sum(1 for cid in cv if cid not in seen)
            st.new_channels = new_cnt
            if st.unique_channels > 0:
                st.novelty = new_cnt / st.unique_channels
                low_ratio = st.low_view_channels / st.unique_channels
                result_ratio = min(1.0, st.result_count / max(1, self.settings.results_per_query))
                st.score = 0.50 * st.novelty + 0.30 * low_ratio + 0.20 * result_ratio
                self.pool.record(st.norm, st.score)
            # persistent adaptive priority (EMA от new_ratio)
            prev = self.priorities.get(st.norm, {"runs": 0, "ema": 0.5})
            st.runs = prev["runs"] + 1
            st.new_ratio = new_cnt / max(1, st.unique_channels)
            st.priority_ema = 0.3 * st.new_ratio + 0.7 * prev["ema"]
            self.priorities[st.norm] = {"runs": st.runs, "ema": st.priority_ema}

        channel_ids = list(channel_views.keys())
        log.info("discovery: %d уникальных каналов из %d запросов", len(channel_ids), len(queries))
        if not channel_ids:
            return []

        # novelty по каналам: канал новый, если его нет в seen (persistent)
        new_cnt_total = sum(1 for c in channel_ids if c not in seen)
        log.info("discovery: %d новых каналов (из %d)", new_cnt_total, len(channel_ids))

        channels_raw = self.youtube.get_channels(channel_ids)
        out: list[ChannelCandidate] = []
        for cid in channel_ids:
            ch = channels_raw.get(cid)
            if not ch:
                continue
            snippet = ch.get("snippet", {})
            stats = ch.get("statistics", {})
            content = ch.get("contentDetails", {})
            video_count = int(stats.get("videoCount", 0) or 0)
            if video_count < settings.min_channel_videos:
                continue
            custom_url = snippet.get("customUrl", "") or ""
            handle = ""
            if "@" in custom_url:
                handle = custom_url.split("@", 1)[1].split("/")[0].strip()
            subs = int(stats.get("subscriberCount", 0) or 0)
            views = channel_views.get(cid, [0])
            score = _candidate_score(subs, views)
            out.append(ChannelCandidate(
                channel_id=cid,
                title=snippet.get("title", "") or channel_titles.get(cid, ""),
                custom_url=custom_url,
                handle=handle,
                subscriber_count=subs,
                video_count=video_count,
                uploads_playlist_id=(content.get("relatedPlaylists", {}) or {}).get("uploads", ""),
                country=snippet.get("country", "") or "",
                popularity_score=score,
            ))
        out.sort(key=lambda c: c.popularity_score)

        _save_seen_channels(settings, seen | set(channel_ids)) if save_seen else None
        self._save_query_stats()
        self._save_priorities()
        self._save_aggregate_stats()

        log.info("discovery: %d каналов после фильтра (>=%d видео)", len(out), settings.min_channel_videos)
        return out

    def _save_query_stats(self) -> None:
        p = Path(self.settings.logs_dir) / "query_stats.json"
        try:
            data = {k: v.to_dict() for k, v in self.stats.items()}
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _save_aggregate_stats(self) -> None:
        """Агрегирует language/theme/subtheme статистику в отдельные JSON."""
        logs = Path(self.settings.logs_dir)

        def _agg(key_fn):
            out: dict[str, dict] = {}
            for st in self.stats.values():
                key = key_fn(st) or "?"
                d = out.setdefault(key, {"queries": 0, "unique_channels": 0, "new_channels": 0})
                d["queries"] += 1
                d["unique_channels"] += st.unique_channels
                d["new_channels"] += st.new_channels
            for d in out.values():
                d["new_ratio"] = round(d["new_channels"] / max(1, d["unique_channels"]), 3)
            return out

        try:
            (logs / "language_stats.json").write_text(
                json.dumps(_agg(lambda s: s.language), ensure_ascii=False, indent=2), encoding="utf-8")
            (logs / "theme_stats.json").write_text(
                json.dumps(_agg(lambda s: s.theme), ensure_ascii=False, indent=2), encoding="utf-8")
            (logs / "subtheme_stats.json").write_text(
                json.dumps(_agg(lambda s: s.subtheme), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def report(self) -> str:
        if not self.stats:
            return "нет статистики"
        ranked = sorted(self.stats.values(), key=lambda s: s.score, reverse=True)
        lines = ["\n=== TOP QUERIES ==="]
        for s in ranked[:8]:
            lines.append(f"  {s.query[:58]:58s} score={s.score:.2f} ch={s.unique_channels} low={s.low_view_channels} err={s.errors}")
        lines.append("=== BOTTOM QUERIES ===")
        for s in ranked[-5:]:
            lines.append(f"  {s.query[:58]:58s} score={s.score:.2f} ch={s.unique_channels} err={s.errors}")
        return "\n".join(lines)


def discover_channels_ytdlp(settings, youtube, max_per_query: Optional[int] = None) -> list[ChannelCandidate]:
    """Backward-compatible entry: параллельный discovery со скорингом."""
    if max_per_query and max_per_query != settings.results_per_query:
        settings.results_per_query = max_per_query
    engine = DiscoveryEngine(settings, youtube)
    return engine.run()
