"""Настройка логирования: файл + консоль + отдельный лог истории."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Консоль Windows по умолчанию использует cp1251; переводим на UTF-8,
    # чтобы логи с символами других языков (ä/ü/ñ/é) не падали при выводе.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Приглушаем служебные логгеры, чтобы не шумели в консоли.
    for noisy in ("httpx", "httpcore", "googleapiclient", "googleapiclient.discovery_cache", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Основной лог
    fh = RotatingFileHandler(logs_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(level)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(level)
    root.addHandler(ch)

    # Лог истории работы (кто отправлен / пропущен / почему)
    hist = logging.getLogger("history")
    hist.setLevel(logging.INFO)
    hist.propagate = False
    hh = RotatingFileHandler(logs_dir / "history.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    hh.setFormatter(fmt)
    hist.addHandler(hh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
