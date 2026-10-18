# -*- coding: utf-8 -*-
"""Контроль доступа к YouTube-видеопотоку.

Содержит ТОЛЬКО логику снижения нагрузки и корректного останова/восстановления:
- классификация ошибок (bot-check / age-gate / unavailable / 403 / expire / network);
- глобальный state machine NORMAL → SUSPECTED → BLOCKED → RECOVERING;
- exponential backoff кулдауна (base → base*factor → ... → cap);
- per-video cooldown для 403/expire (не долбить одно видео);
- метрики probe/frame (счётчики + тайминги).

НЕ обходит защиту YouTube, не подменяет клиент, не решает капчу.
"""
from __future__ import annotations

import threading
import time

# --------------------------------------------------------------------------- #
# Виды ошибок
# --------------------------------------------------------------------------- #
BOT_CHECK = "bot_check"        # «Sign in to confirm you're not a bot»
AGE_GATE = "age_gate"          # «Sign in to confirm your age»
UNAVAILABLE = "unavailable"    # private / deleted / unavailable
FORBIDDEN = "forbidden"        # HTTP 403 от CDN / googlevideo
URL_EXPIRED = "url_expired"    # истёк signature/expire
NETWORK = "network"            # timeout / DNS / reset / 5xx / connection
UNSUPPORTED = "unsupported"    # нет доступного формата
UNKNOWN = "unknown"

# Временные ошибки: канал НЕ помечается проверенным, разрешён повтор после cooldown.
TEMP_KINDS = frozenset({BOT_CHECK, FORBIDDEN, URL_EXPIRED, NETWORK})
# Постоянные: видео/канал можно считать отклонённым.
PERMANENT_KINDS = frozenset({UNAVAILABLE, AGE_GATE, UNSUPPORTED})
# «Не просматриваемые»: НЕ считаются ни PASS, ни FAIL, исключаются из denominator 30%.
UNWATCHABLE_KINDS = frozenset({UNAVAILABLE, AGE_GATE})


def classify_youtube_error(message: str) -> str:
    """Классифицирует текст ошибки yt-dlp / ffmpeg по виду."""
    msg = (message or "").lower()
    if not msg:
        return UNKNOWN
    if "sign in to confirm your age" in msg:
        return AGE_GATE
    if "not a bot" in msg or ("sign in to confirm" in msg and "age" not in msg):
        return BOT_CHECK
    if ("this video is not available" in msg or "video unavailable" in msg
            or "private video" in msg or "removed by the uploader" in msg
            or "has been removed" in msg or "deleted" in msg):
        return UNAVAILABLE
    if "signature" in msg or "expired" in msg or "url expired" in msg:
        return URL_EXPIRED
    if ("http error 403" in msg or " 403 " in msg or msg.startswith("403")
            or "403 forbidden" in msg or "server returned 403" in msg):
        return FORBIDDEN
    if ("no video formats" in msg or "requested format is not available" in msg
            or "unsupported" in msg or "no formats found" in msg):
        return UNSUPPORTED
    for m in ("timeout", "timed out", "dns", "connection", "reset",
              "unreachable", "refused", "network", "ssl", "closed",
              "500", "502", "503", "504", "eof", "end of file"):
        if m in msg:
            return NETWORK
    return UNKNOWN


class YouTubeAccessController:
    """Глобальное состояние доступа к YouTube-потоку + backoff кулдауна."""

    NORMAL = "normal"
    SUSPECTED = "suspected"
    BLOCKED = "blocked"
    RECOVERING = "recovering"

    def __init__(self, base_cooldown: float = 1800.0, backoff_factor: float = 2.0,
                 max_cooldown: float = 7200.0):
        self.base_cooldown = float(base_cooldown)
        self.backoff_factor = float(backoff_factor)
        self.max_cooldown = float(max_cooldown)
        self.state = self.NORMAL
        self.current_cooldown = float(base_cooldown)
        self.cooldown_until = 0.0
        self.block_count = 0
        self._lock = threading.Lock()

    def is_blocked(self) -> bool:
        return time.time() < self.cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def note_suspected(self) -> None:
        """Один странный 403/необычный ответ, но не точный bot-check."""
        with self._lock:
            if self.state == self.NORMAL:
                self.state = self.SUSPECTED

    def note_bot_check(self) -> None:
        """Точный bot-check: BLOCKED + кулдаун с exponential backoff."""
        with self._lock:
            self.block_count += 1
            if self.state != self.BLOCKED:
                # первый блок в серии — начинаем с base
                self.current_cooldown = self.base_cooldown
            else:
                # повторный блок — увеличиваем, но не выше cap
                self.current_cooldown = min(
                    self.max_cooldown,
                    self.current_cooldown * self.backoff_factor,
                )
            self.cooldown_until = time.time() + self.current_cooldown
            self.state = self.BLOCKED

    def note_success(self) -> None:
        """Успешный probe — полное восстановление."""
        with self._lock:
            self.state = self.NORMAL
            self.current_cooldown = self.base_cooldown
            self.cooldown_until = 0.0

    def start_recovery_probe(self) -> bool:
        """Пытается начать осторожный recovery-пробник после окончания кулдауна.

        Возвращает True, если пробник разрешён (cooldown истёк), иначе False.
        """
        with self._lock:
            if time.time() < self.cooldown_until:
                return False
            if self.state == self.BLOCKED:
                self.state = self.RECOVERING
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "blocked": self.is_blocked(),
                "cooldown_remaining_s": int(self.cooldown_remaining()),
                "current_cooldown_s": int(self.current_cooldown),
                "block_count": self.block_count,
            }


class VideoErrorCooldown:
    """Per-video кулдаун для временных ошибок (403/expire/network).

    Позволяет НЕ долбить одно и то же видео подряд, продолжая остальные.
    """

    def __init__(self, cooldown_sec: float = 600.0):
        self.cooldown_sec = float(cooldown_sec)
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_blocked(self, video_id: str) -> bool:
        with self._lock:
            return time.time() < self._until.get(video_id, 0.0)

    def mark(self, video_id: str) -> None:
        with self._lock:
            self._until[video_id] = time.time() + self.cooldown_sec

    def clear(self, video_id: str) -> None:
        with self._lock:
            self._until.pop(video_id, None)

    def prune(self, max_entries: int = 10000) -> None:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._until.items() if v < now]
            for k in expired:
                del self._until[k]
            if len(self._until) > max_entries:
                # удаляем самые старые по сроку окончания
                for k in sorted(self._until, key=self._until.get)[: len(self._until) - max_entries]:
                    del self._until[k]


class AccessMetrics:
    """Счётчики и тайминги probe/frame (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.probe_total = 0
        self.probe_ok = 0
        self.probe_fail = 0
        self.probe_by_kind: dict[str, int] = {}
        self.probe_times: list[float] = []
        self.frame_total = 0
        self.frame_ok = 0
        self.frame_fail = 0
        self.frame_by_kind: dict[str, int] = {}
        self.frame_times: list[float] = []
        self.reprobes = 0

    def record_probe(self, ok: bool, kind: str, duration: float) -> None:
        with self._lock:
            self.probe_total += 1
            self.probe_times.append(duration)
            if ok:
                self.probe_ok += 1
            else:
                self.probe_fail += 1
                self.probe_by_kind[kind] = self.probe_by_kind.get(kind, 0) + 1

    def record_frame(self, ok: bool, duration: float, kind: str = "") -> None:
        with self._lock:
            self.frame_total += 1
            self.frame_times.append(duration)
            if ok:
                self.frame_ok += 1
            else:
                self.frame_fail += 1
                if kind:
                    self.frame_by_kind[kind] = self.frame_by_kind.get(kind, 0) + 1

    def record_reprobe(self) -> None:
        with self._lock:
            self.reprobes += 1

    def _p95(self, xs: list[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        idx = min(len(s) - 1, int(len(s) * 0.95))
        return s[idx]

    def summary(self) -> dict:
        with self._lock:
            probe_times = list(self.probe_times)
            frame_times = list(self.frame_times)
            return {
                "probe_total": self.probe_total,
                "probe_ok": self.probe_ok,
                "probe_fail": self.probe_fail,
                "probe_success_rate": round(self.probe_ok / max(1, self.probe_total), 3),
                "probe_by_kind": dict(self.probe_by_kind),
                "probe_avg_s": round(sum(probe_times) / max(1, len(probe_times)), 3),
                "probe_p95_s": round(self._p95(probe_times), 3),
                "frame_total": self.frame_total,
                "frame_ok": self.frame_ok,
                "frame_fail": self.frame_fail,
                "frame_success_rate": round(self.frame_ok / max(1, self.frame_total), 3),
                "frame_by_kind": dict(self.frame_by_kind),
                "frame_avg_s": round(sum(frame_times) / max(1, len(frame_times)), 3),
                "frame_p95_s": round(self._p95(frame_times), 3),
                "reprobes": self.reprobes,
            }


# Единый глобальный контроллер доступа (импортируется остальными модулями).
access_controller = YouTubeAccessController()
video_cooldown = VideoErrorCooldown()
access_metrics = AccessMetrics()


def configure_access_control(
    base_cooldown: float = 1800.0,
    backoff_factor: float = 2.0,
    max_cooldown: float = 7200.0,
    video_error_cooldown: float = 600.0,
) -> None:
    """Настраивает глобальный контроллер из Settings (вызывается при старте)."""
    access_controller.base_cooldown = float(base_cooldown)
    access_controller.backoff_factor = float(backoff_factor)
    access_controller.max_cooldown = float(max_cooldown)
    if access_controller.state == access_controller.NORMAL:
        access_controller.current_cooldown = float(base_cooldown)
    video_cooldown.cooldown_sec = float(video_error_cooldown)


# --------------------------------------------------------------------------- #
# Adaptive MiMo concurrency (отдельный limiter, НЕ смешан с discovery).
# Живёт на уровне модуля: переживает пересоздание evaluator/mimo между попытками.
# --------------------------------------------------------------------------- #
_mimo_parallel = {"current": 2, "base": 2, "clean": 0, "min": 2}


def init_mimo_parallel(base: int) -> None:
    """Устанавливает базовый параллелизм (первый вызов)."""
    base = max(2, int(base))
    if _mimo_parallel["base"] == 2 and _mimo_parallel["current"] == 2:
        _mimo_parallel["base"] = base
        _mimo_parallel["current"] = base


def reset_mimo_parallel(base: int = 2, min_par: int = 2) -> None:
    """Сброс адаптивного параллелизма (для тестов)."""
    _mimo_parallel["current"] = base
    _mimo_parallel["base"] = base
    _mimo_parallel["min"] = min_par
    _mimo_parallel["clean"] = 0


# --------------------------------------------------------------------------- #
# MiMo rate-limit (429): глобальный cooldown с exponential backoff.
# 429 = исчерпание токенов ИЛИ RPM-лимит. При повторных 429 растягиваем паузу:
# 1800 -> 3600 -> 7200 (cap), при успехе сбрасываем.
# --------------------------------------------------------------------------- #
_mimo_rl_until = 0.0
_mimo_rl_streak = 0


def mimo_is_rate_limited() -> bool:
    return time.time() < _mimo_rl_until


def mimo_note_rate_limit() -> None:
    """Фиксирует 429 и увеличивает паузу (backoff)."""
    global _mimo_rl_until, _mimo_rl_streak
    _mimo_rl_streak += 1
    cooldown = min(7200.0, 1800.0 * (2 ** min(_mimo_rl_streak - 1, 2)))
    _mimo_rl_until = time.time() + cooldown


def mimo_note_success() -> None:
    """Успешный MiMo-вызов сбрасывает streak (лимит остыл)."""
    global _mimo_rl_streak
    _mimo_rl_streak = 0


def current_mimo_parallel() -> int:
    return max(_mimo_parallel["min"], _mimo_parallel["current"])


def adjust_mimo_parallel(errors: int, calls: int) -> int:
    """После канала: ошибки MiMo -> снижаем, стабильность -> возвращаем."""
    cur = _mimo_parallel["current"]
    base = _mimo_parallel["base"]
    if errors > 0:
        _mimo_parallel["current"] = max(_mimo_parallel["min"], cur - 1)
        _mimo_parallel["clean"] = 0
    else:
        _mimo_parallel["clean"] += 1
        if cur < base and _mimo_parallel["clean"] >= 3:
            _mimo_parallel["current"] = min(base, cur + 1)
            _mimo_parallel["clean"] = 0
    return _mimo_parallel["current"]
