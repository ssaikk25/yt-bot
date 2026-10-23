# -*- coding: utf-8 -*-
"""Контроль доступа к YouTube-видеопотоку.

Содержит ТОЛЬКО логику снижения нагрузки и корректного останова/восстановления:
- классификация ошибок с учётом источника (youtube.com / googlevideo / Data API);
- глобальный state machine NORMAL → SUSPECTED → BLOCKED → RECOVERING → NORMAL;
- exponential backoff кулдауна (base → base*factor → ... → cap);
- персистентность состояния между рестартами;
- единый гейт `youtube_gate()` для ВСЕХ прямых запросов к youtube.com;
- per-video cooldown для 403/expire (не долбить одно видео);
- метрики probe/frame (счётчики + тайминги).

НЕ обходит защиту YouTube, не подменяет клиент, не решает капчу.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("access")

# --------------------------------------------------------------------------- #
# Виды ошибок
# --------------------------------------------------------------------------- #
BOT_CHECK = "bot_check"        # явный «Sign in to confirm you're not a bot»
AGE_GATE = "age_gate"          # «Sign in to confirm your age»
UNAVAILABLE = "unavailable"    # private / deleted / unavailable
PAGE_403 = "page_403"          # HTTP 403 от youtube.com (страница/API-страница)
GVS_403 = "gvs_403"            # HTTP 403 от googlevideo.com (медиапоток)
OTHER_403 = "other_403"        # HTTP 403 из неопознанного источника
URL_EXPIRED = "url_expired"    # истёк signature/expire
NETWORK = "network"            # timeout / DNS / reset / 5xx / connection
UNSUPPORTED = "unsupported"    # нет доступного формата
API_QUOTA = "api_quota"        # Data API: квота исчерпана
API_AUTH = "api_auth"          # Data API: 401/403 ключ
API_TEMP = "api_temp"          # Data API: временная 5xx/сеть
UNKNOWN = "unknown"

# Обратная совместимость: старые коды.
FORBIDDEN = OTHER_403

# Временные ошибки: канал НЕ помечается проверенным, разрешён повтор после cooldown.
TEMP_KINDS = frozenset({
    BOT_CHECK, PAGE_403, GVS_403, OTHER_403, URL_EXPIRED, NETWORK, API_TEMP,
})
# Постоянные: видео/канал можно считать отклонённым.
PERMANENT_KINDS = frozenset({UNAVAILABLE, AGE_GATE, UNSUPPORTED})
# «Не просматриваемые»: НЕ считаются ни PASS, ни FAIL, исключаются из denominator 30%.
UNWATCHABLE_KINDS = frozenset({UNAVAILABLE, AGE_GATE})
# 403 от медиапотока — НЕ глобальный bot-check; лечим per-video cooldown.
MEDIA_KINDS = frozenset({GVS_403, URL_EXPIRED})

_BOT_MARKERS = (
    "not a bot",
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "bot-check",
    "bot check",
)
_403_MARKERS = ("http error 403", "403 forbidden", "server returned 403", " 403 ")


def classify_youtube_error(message: str, source: str = "") -> str:
    """Классифицирует текст ошибки yt-dlp / ffmpeg / Data API по виду.

    source: "youtube" | "googlevideo" | "api" | "" — уточняет 403 и сетевые ошибки,
    чтобы ошибка одного компонента не меняла состояние другого.
    """
    msg = (message or "").lower()
    if not msg:
        return UNKNOWN
    src = (source or "").lower()

    # Data API имеет собственную классификацию.
    if src == "api":
        if "quota" in msg or "quotaexceeded" in msg:
            return API_QUOTA
        if ("api key" in msg or "keyinvalid" in msg or "unauthorized" in msg
                or "401" in msg or "forbidden" in msg and "quota" not in msg):
            return API_AUTH
        for m in ("timeout", "timed out", "connection", "reset", "503", "502",
                  "500", "504", "unavailable", "backend"):
            if m in msg:
                return API_TEMP
        return API_TEMP

    if "sign in to confirm your age" in msg:
        return AGE_GATE
    if "not a bot" in msg or ("sign in to confirm" in msg and "age" not in msg):
        return BOT_CHECK
    if ("this video is not available" in msg or "video unavailable" in msg
            or "private video" in msg or "removed by the uploader" in msg
            or "has been removed" in msg or "deleted" in msg):
        return UNAVAILABLE
    if "signature" in msg or "url expired" in msg or "expired" in msg:
        return URL_EXPIRED
    if any(m in msg for m in _403_MARKERS) or msg.startswith("403"):
        if src == "googlevideo":
            return GVS_403
        if src in ("youtube", "page"):
            return PAGE_403
        return OTHER_403
    if ("no video formats" in msg or "requested format is not available" in msg
            or "no formats found" in msg):
        return UNSUPPORTED
    for m in ("timeout", "timed out", "dns", "connection", "reset",
              "unreachable", "refused", "network", "ssl", "closed",
              "500", "502", "503", "504", "eof", "end of file"):
        if m in msg:
            return NETWORK
    return UNKNOWN


class BotBlockedError(RuntimeError):
    """Запрос к youtube.com запрещён: активен глобальный cooldown."""

    def __init__(self, component: str, remaining: float):
        self.component = component
        self.remaining = float(remaining)
        super().__init__(
            f"global bot-block активен: {component}, осталось {int(remaining)} c"
        )


class YouTubeAccessController:
    """Глобальное состояние доступа к YouTube-потоку + backoff кулдауна."""

    NORMAL = "normal"
    SUSPECTED = "suspected"
    BLOCKED = "blocked"
    RECOVERING = "recovering"

    # Параллельные probe, получившие bot-check одновременно, — это ОДИН эпизод.
    # В пределах окна backoff не эскалируется повторно.
    DEDUP_WINDOW = 60.0
    # Сколько УСПЕШНЫХ probe (реальной чистой работы) нужно, чтобы сбросить backoff.
    RESET_AFTER_PROBES = 50

    def __init__(self, base_cooldown: float = 1800.0, backoff_factor: float = 2.0,
                 max_cooldown: float = 7200.0):
        self.base_cooldown = float(base_cooldown)
        self.backoff_factor = float(backoff_factor)
        self.max_cooldown = float(max_cooldown)
        self.state = self.NORMAL
        self.current_cooldown = float(base_cooldown)
        self.cooldown_until = 0.0
        self.block_count = 0
        self._state_path: Optional[Path] = None
        self._lock = threading.Lock()
        self._last_remaining_log = 0.0
        self._last_block_mono = 0.0
        self._last_block_at = 0.0
        self._success_since_block = 0

    # ---------------------------------------------------------------- #
    def is_blocked(self) -> bool:
        return time.time() < self.cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def _set_state(self, new_state: str, reason: str = "") -> None:
        old = self.state
        self.state = new_state
        if old != new_state:
            log.info("ACCESS_STATE_CHANGED %s -> %s (%s)", old, new_state, reason)

    # ---------------------------------------------------------------- #
    def note_suspected(self, reason: str = "") -> None:
        """Один странный 403/необычный ответ, но не точный bot-check."""
        with self._lock:
            if self.state == self.NORMAL:
                self._set_state(self.SUSPECTED, reason or "non-fatal 403")

    def note_bot_check(self, source: str = "") -> None:
        """ТОЛЬКО явный bot-check: BLOCKED + кулдаун с exponential backoff.

        Дедуп: один bot-check = один эпизод (параллельные probe не эскалируют многократно).
        Эскалация по времени: если с ПРОШЛОГО блока прошло < RESET_AFTER, backoff
        растёт — даже если между ними был успешный recovery-probe. Иначе (долгая
        чистая работа) начинаем с base_cooldown.
        """
        with self._lock:
            now_mono = time.monotonic()
            if (self.state == self.BLOCKED
                    and (now_mono - self._last_block_mono) < self.DEDUP_WINDOW):
                log.info("BOT_CHECK_DEDUPED source=%s (эпизод уже учтён, "
                         "осталось %d c)", source or "unknown",
                         int(self.cooldown_remaining()))
                return
            since_last = (now_mono - self._last_block_mono
                          if self._last_block_mono else 1e9)
            was_recovering = self.state == self.RECOVERING
            # Эскалация: повторный блок, если после прошлого блока НЕ было
            # достаточно чистой работы. Recovery-probe сам по себе не считается работой.
            if self.block_count > 0 and self._success_since_block < self.RESET_AFTER_PROBES:
                self.current_cooldown = min(
                    self.max_cooldown,
                    self.current_cooldown * self.backoff_factor,
                )
            else:
                self.current_cooldown = self.base_cooldown
            self._last_block_mono = now_mono
            self._last_block_at = time.time()
            self._success_since_block = 0
            self.block_count += 1
            self.cooldown_until = time.time() + self.current_cooldown
            self._set_state(self.BLOCKED, f"bot_check from {source or 'unknown'}")
            log.warning("BOT_CHECK_CONFIRMED source=%s block_count=%d success_since_block=%d",
                        source or "unknown", self.block_count, self._success_since_block)
            if was_recovering:
                log.warning("RECOVERY_PROBE_BLOCKED source=%s", source or "unknown")
            log.info("COOLDOWN_STARTED %d c (until unixtime=%d)",
                     int(self.current_cooldown), int(self.cooldown_until))
            self._save_locked()

    def note_success(self) -> None:
        """Успешный probe — состояние NORMAL; backoff сбрасывается после N успехов.

        Один успешный recovery-probe НЕ должен обнулять backoff: иначе бот каждые
        полчаса выходит и снова ловит блок с тем же коротким кулдауном.
        """
        with self._lock:
            self._set_state(self.NORMAL, "probe success")
            self.cooldown_until = 0.0
            self._success_since_block += 1
            if (self._success_since_block >= self.RESET_AFTER_PROBES
                    and self.current_cooldown > self.base_cooldown):
                log.info("BACKOFF_RESET после %d успешных probe", self._success_since_block)
                self.current_cooldown = self.base_cooldown
                self._success_since_block = 0
            self._save_locked()

    def start_recovery_probe(self) -> bool:
        """Разрешает ОДИН осторожный recovery-пробник после окончания кулдауна."""
        with self._lock:
            if time.time() < self.cooldown_until:
                return False
            if self.state == self.BLOCKED:
                log.info("COOLDOWN_EXPIRED")
                self._set_state(self.RECOVERING, "cooldown expired")
                log.info("RECOVERY_PROBE_STARTED")
            return True

    def mark_recovery_succeeded(self) -> None:
        with self._lock:
            log.info("RECOVERY_PROBE_SUCCEEDED")
            self._set_state(self.NORMAL, "recovery ok")
            # Backoff не сбрасываем: сброс — только после RESET_AFTER чистой работы.
            self.cooldown_until = 0.0
            self._save_locked()

    # ---------------------------------------------------------------- #
    def gate(self, component: str) -> None:
        """Единая точка проверки для ЛЮБОГО прямого запроса к youtube.com."""
        if self.is_blocked():
            remaining = self.cooldown_remaining()
            if component == "discovery":
                log.info("YOUTUBE_DISCOVERY_PAUSED %d c", int(remaining))
            elif component == "probe":
                log.info("YOUTUBE_PROBE_PAUSED %d c", int(remaining))
            raise BotBlockedError(component, remaining)

    # ---------------------------------------------------------------- #
    # Персистентность состояния между рестартами.
    # ---------------------------------------------------------------- #
    def bind_state_path(self, path) -> None:
        self._state_path = Path(path)

    def _save_locked(self) -> None:
        if not self._state_path:
            return
        try:
            data = {
                "state": self.state,
                "cooldown_until": self.cooldown_until,
                "current_cooldown": self.current_cooldown,
                "block_count": self.block_count,
                "last_block_at": self._last_block_at,
                "success_since_block": self._success_since_block,
                "saved_at": time.time(),
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            log.warning("ACCESS_STATE_SAVE_FAILED %s", e)

    def load_state(self) -> bool:
        """Восстанавливает состояние после рестарта. True, если состояние было."""
        if not self._state_path or not self._state_path.exists():
            return False
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        with self._lock:
            self.state = data.get("state", self.NORMAL)
            self.cooldown_until = float(data.get("cooldown_until", 0.0) or 0.0)
            self.current_cooldown = float(
                data.get("current_cooldown", self.base_cooldown) or self.base_cooldown)
            self.block_count = int(data.get("block_count", 0) or 0)
            self._last_block_at = float(data.get("last_block_at", 0.0) or 0.0)
            self._success_since_block = int(data.get("success_since_block", 0) or 0)
            # Восстанавливаем "сколько назад был прошлый блок" для эскалации backoff.
            if self._last_block_at:
                ago = max(0.0, time.time() - self._last_block_at)
                self._last_block_mono = time.monotonic() - ago
            if self.is_blocked():
                log.info("ACCESS_STATE_RESTORED state=%s cooldown_remaining=%d c",
                         self.state, int(self.cooldown_remaining()))
            elif self.state == self.BLOCKED:
                # кулдаун истёк, пока процесс был остановлен — нужен recovery probe
                self.state = self.RECOVERING
                log.info("ACCESS_STATE_RESTORED state=recovering (cooldown истёк)")
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
    """Per-video кулдаун для временных ошибок (403/expire/network)."""

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
    state_path=None,
) -> None:
    """Настраивает глобальный контроллер из Settings (вызывается при старте)."""
    access_controller.base_cooldown = float(base_cooldown)
    access_controller.backoff_factor = float(backoff_factor)
    access_controller.max_cooldown = float(max_cooldown)
    if access_controller.state == access_controller.NORMAL:
        access_controller.current_cooldown = float(base_cooldown)
    video_cooldown.cooldown_sec = float(video_error_cooldown)
    if state_path:
        access_controller.bind_state_path(state_path)


def youtube_gate(component: str) -> None:
    """Публичный гейт: бросает BotBlockedError, если активен cooldown."""
    access_controller.gate(component)


# Обратная совместимость.
def is_bot_blocked() -> bool:
    return access_controller.is_blocked()


# --------------------------------------------------------------------------- #
# Adaptive MiMo concurrency (отдельный limiter, НЕ смешан с discovery).
# --------------------------------------------------------------------------- #
_mimo_parallel = {"current": 2, "base": 2, "clean": 0, "min": 2}


def init_mimo_parallel(base: int) -> None:
    base = max(2, int(base))
    if _mimo_parallel["base"] == 2 and _mimo_parallel["current"] == 2:
        _mimo_parallel["base"] = base
        _mimo_parallel["current"] = base


def reset_mimo_parallel(base: int = 2, min_par: int = 2) -> None:
    _mimo_parallel["current"] = base
    _mimo_parallel["base"] = base
    _mimo_parallel["min"] = min_par
    _mimo_parallel["clean"] = 0


# --------------------------------------------------------------------------- #
# MiMo rate-limit (429): глобальный cooldown с exponential backoff.
# --------------------------------------------------------------------------- #
_mimo_rl_until = 0.0
_mimo_rl_streak = 0


def mimo_is_rate_limited() -> bool:
    return time.time() < _mimo_rl_until


def mimo_note_rate_limit() -> None:
    global _mimo_rl_until, _mimo_rl_streak
    _mimo_rl_streak += 1
    cooldown = min(7200.0, 1800.0 * (2 ** min(_mimo_rl_streak - 1, 2)))
    _mimo_rl_until = time.time() + cooldown


def mimo_note_success() -> None:
    global _mimo_rl_streak
    _mimo_rl_streak = 0


def current_mimo_parallel() -> int:
    return max(_mimo_parallel["min"], _mimo_parallel["current"])


def adjust_mimo_parallel(errors: int, calls: int) -> int:
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
