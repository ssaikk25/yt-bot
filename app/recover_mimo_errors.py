# -*- coding: utf-8 -*-
"""Разовое восстановление: вернуть в очередь каналы, чьи видео были ошибочно
отклонены из-за MiMo-ошибок (429/исчерпание токенов/connection reset).

Логика:
1. парсим bot.log — все строки «ошибка MiMo», достаём video_id;
2. через YouTube Data API (videos.list, batch 50) маппим video_id -> channel_id;
3. убираем эти channel_id из checked_channels.json (с бэкапом), чтобы после
   пополнения токенов раннер повторно оценил эти каналы.

Запуск: python -m app.recover_mimo_errors
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from .config import load_settings
from .youtube_client import YouTubeClient

VIDEO_RE = re.compile(r"видео ([A-Za-z0-9_-]{11})")
TITLE_RE = re.compile(r"Канал (.+?): видео")


def parse_mimo_error_videos(log_path: Path) -> dict[str, str]:
    """video_id -> channel_title для всех видео с «ошибка MiMo»."""
    out: dict[str, str] = {}
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if "ошибка MiMo" not in line:
                continue
            m = VIDEO_RE.search(line)
            if m:
                vid = m.group(1)
                t = TITLE_RE.search(line)
                out[vid] = t.group(1).strip() if t else ""
    except Exception as e:
        print(f"Ошибка чтения лога: {e}")
    return out


def main() -> int:
    settings = load_settings()
    logs = Path(settings.logs_dir)
    log_path = logs / "bot.log"
    checked_path = logs / "checked_channels.json"

    video_titles = parse_mimo_error_videos(log_path)
    video_ids = list(video_titles.keys())
    print(f"Видео с MiMo-ошибкой в логе: {len(video_ids)}")

    if not video_ids:
        print("Ничего не найдено.")
        return 0

    # маппинг video -> channel через Data API (batch 50)
    yt = YouTubeClient(settings.youtube_api_key, settings.youtube_quota_budget)
    details = yt.get_video_details(video_ids)

    channel_ids: set[str] = set()
    unmapped = 0
    for vid in video_ids:
        d = details.get(vid)
        if d and (d.get("snippet") or {}).get("channelId"):
            channel_ids.add(d["snippet"]["channelId"])
        else:
            unmapped += 1
            print(f"  ! не удалось маппить (удалено/недоступно): {vid} | {video_titles[vid]}")

    print(f"Каналов к возврату: {len(channel_ids)} (не маппится: {unmapped})")

    # бэкап + убираем из checked
    checked: list[str] = []
    if checked_path.exists():
        checked = json.loads(checked_path.read_text(encoding="utf-8"))
        if isinstance(checked, list):
            bak = checked_path.with_suffix(".bak")
            shutil.copy2(checked_path, bak)
            print(f"Бэкап: {bak}")

    before = len(checked)
    checked = [c for c in checked if c not in channel_ids]
    removed = before - len(checked)
    checked_path.write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Убрано из checked_channels.json: {removed} (было {before}, стало {len(checked)})")
    print("Готово. После пополнения токенов перезапусти раннер — эти каналы вернутся в очередь.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
