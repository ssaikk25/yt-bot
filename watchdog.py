# -*- coding: utf-8 -*-
"""Автономный сторож (watchdog) раннера overnight.

Каждые CHECK_INTERVAL секунд:
- проверяет, что процесс python -m app.overnight жив;
- проверяет, что bot.log менялся недавно (раннер пишет туда на каждый шаг:
  discovery, оценку видео, bot-block-паузы). Если лог не менялся > STUCK_TIMEOUT —
  процесс реально завис, перезапускаем;
- если процесс упал — перезапускаем.

Работает независимо от чата. Запуск:  python watchdog.py

ВАЖНО: сигнал прогресса — mtime bot.log, а НЕ found/checked json (те меняются
только при находке/проверке канала, что даёт ложные «зависания»).
"""
import os
import subprocess

from app import subproc
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
BOT_LOG = LOGS / "bot.log"
WLOG = LOGS / "watchdog.log"

CHECK_INTERVAL = 600          # 10 минут
STUCK_TIMEOUT = 7200          # 120 минут без строки в bot.log = завис (discovery бывает тихим ~74 мин)
STARTUP_GRACE = 180           # 3 мин после запуска/рестарта не убиваем (раза с гонкой старта)


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    try:
        with open(WLOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def mtime_or_none(p: Path):
    try:
        return p.stat().st_mtime
    except Exception:
        return None


def overnight_pid() -> int:
    """Находит PID процесса python -m app.overnight."""
    try:
        out = subproc.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*app.overnight*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
        pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return pids[0] if pids else 0
    except Exception:
        return 0


def start_runner():
    log("ЗАПУСК python -m app.overnight")
    env = os.environ.copy()
    env.update({
        "MAX_VIEWS": "10000", "TARGET_CHANNELS": "999999999", "MAX_HOURS": "48",
        "QUOTA_WAIT_SECONDS": "900", "MAX_VIDEOS_PER_CHANNEL": "20",
        "EVAL_PARALLEL": "6", "YTDLP_DELAY": "3.0",
        "PROBE_MIN_INTERVAL": "0",
        "FRAME_COUNT": "5", "FRAME_FRACTIONS": "0.05,0.25,0.50,0.75,0.95",
        "QUICK_ACCEPT_CONFIDENCE": "0.90", "DEEP_CONFIDENCE_THRESHOLD": "0.90",
        "DISCOVERY_WORKERS": "4", "MAX_DISCOVERY_WORKERS": "6",
        "RESULTS_PER_QUERY": "50",
        "FRAME_EXTRACTION_MODE": "multi", "REDISCOVERY_QUEUE_THRESHOLD": "50",
        "TARGET_REGION": "RU", "DISCOVERY_QUERIES_PER_RUN": "240",
        "YOUTUBE_QUOTA_BUDGET": "10000",
    })
    # DETACHED_PROCESS + NEW_PROCESS_GROUP + NO_WINDOW: раннер полностью независим
    flags = 0x00000008 | 0x00000200 | 0x08000000
    subproc.popen(
        [sys.executable, "-m", "app.overnight"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=flags,
    )


def kill_runner(pid: int):
    try:
        subproc.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=30)
        log(f"Убит процесс {pid}")
    except Exception as e:
        log(f"Не удалось убить {pid}: {e}")


def main():
    log(f"=== WATCHDOG запущен (интервал {CHECK_INTERVAL} c, таймаут {STUCK_TIMEOUT} c) ===")
    last_start = time.time()
    while True:
        try:
            pid = overnight_pid()
            lm = mtime_or_none(BOT_LOG)
            now = time.time()

            if pid == 0:
                log("Раннер не найден — запускаю")
                start_runner()
                last_start = now
            elif (now - last_start) > STARTUP_GRACE and lm is not None and (now - lm) > STUCK_TIMEOUT:
                log(f"ЗАВИСАНИЕ: bot.log не менялся {int(now - lm)} c. Перезапуск.")
                kill_runner(pid)
                time.sleep(3)
                start_runner()
                last_start = time.time()
            else:
                age = int(now - lm) if lm is not None else 0
                log(f"OK pid={pid} log_age={age} c")
        except Exception as e:
            log(f"Ошибка watchdog: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
