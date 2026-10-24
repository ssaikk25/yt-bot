# -*- coding: utf-8 -*-
"""Запуск внешних процессов БЕЗ консольных окон (Windows).

Проблема: `subprocess.run(["ffmpeg", ...])` / `["powershell", ...]` на Windows
открывает видимое консольное окно на каждый вызов. При извлечении кадров это
десятки окон. Здесь единая точка: `creationflags=CREATE_NO_WINDOW`.

Если вызывающий код уже передал свой `creationflags` (например, DETACHED_PROCESS),
он НЕ перезаписывается.
"""
from __future__ import annotations

import os
import subprocess


CREATE_NO_WINDOW = 0x08000000


def _apply_no_window(kwargs: dict) -> dict:
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return kwargs


def run(cmd, **kwargs):
    """Аналог subprocess.run без консольного окна."""
    return subprocess.run(cmd, **_apply_no_window(kwargs))


def popen(cmd, **kwargs):
    """Аналог subprocess.Popen без консольного окна."""
    return subprocess.Popen(cmd, **_apply_no_window(kwargs))
