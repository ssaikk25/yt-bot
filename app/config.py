"""Настройки проекта: переменные окружения (.env) + YAML-конфиг поисковых запросов."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .category_mapper import categories_list

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_float_list(name: str, default: list[float]) -> list[float]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return list(default)
    out: list[float] = []
    for item in val.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(float(item))
        except ValueError:
            continue
    return out


def _env_str_list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return list(default)
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass
class Settings:
    # --- Секреты и внешние ключи ---
    youtube_api_key: str = ""
    mimo_api_key: str = ""
    mimo_base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1"  # OpenAI-совместимый endpoint MiMo
    mimo_model: str = "mimo-v2.6-flash"
    mimo_timeout: float = 90.0

    tagme_url: str = "https://tagme.sberdevices.ru/marker/runner/73987d36-e911-4cb5-8013-b5b62d6c203e"

    # --- Пути ---
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    playwright_profile_dir: str = "./browser_profile"
    logs_dir: str = "./logs"
    temp_dir: str = "./temp"
    search_queries_path: str = "./config/search_queries.yaml"

    # --- Поведение ---
    dry_run: bool = True
    headless: bool = False
    slow_mo_ms: int = 0

    # --- Квоты и объёмы ---
    youtube_quota_budget: int = 9000
    max_channels_to_process: int = 10
    max_search_pages_per_query: int = 1
    default_search_max_results: int = 30

    # --- Фильтры каналов ---
    min_channel_videos: int = 10
    min_suitable_ratio: float = 0.30
    min_required_checked: int = 3
    max_videos_per_channel: int = 20
    max_shorts_ratio: float = 0.0  # 0.0 = строго: Shorts/live запрещены
    max_subscribers: int = 0       # 0 = не ограничивать (но приоритет ниже при малых)
    max_views: int = 0             # 0 = не ограничивать

    # --- Фильтры видео ---
    min_duration_seconds: float = 5.0
    max_duration_seconds: float = 4 * 3600.0
    min_height: int = 720
    shorts_max_duration: float = 180.0

    # --- Кадры для MiMo ---
    frame_count: int = 3
    frame_fractions: list[float] = field(default_factory=lambda: [0.10, 0.50, 0.90])  # начало/середина/конец
    frame_fallback_fractions: list[float] = field(default_factory=lambda: [0.30, 0.70])  # для MiMo DEEP
    frame_format: str = "jpg"
    frame_quality: int = 3
    frame_max_width: int = 1280  # кап ширины кадра (экономия токенов MiMo)
    short_video_threshold: float = 30.0  # ниже — равномерное распределение точек
    ytdlp_delay: float = 1.0  # пауза между yt-dlp обращениями (против bot-check)
    pot_server_dir: str = ""  # путь к серверу PO-token (build/main.js)
    cookies_file: str = ""  # путь к cookies.txt YouTube (обходит bot-check)
    eval_parallel: int = 4  # сколько видео канала оценивать параллельно

    # --- Доступ к YouTube-потоку (bot-check / 403 / media_url) ---
    bot_block_cooldown: float = 1800.0     # базовый кулдаун global bot-check, сек
    bot_block_backoff_factor: float = 2.0  # множитель при повторном блоке
    bot_block_max_cooldown: float = 7200.0 # верхний предел кулдауна, сек
    video_403_cooldown: float = 600.0      # per-video cooldown после 403/expire, сек
    frame_extraction_mode: str = "multi"  # "multi" (один ffmpeg-процесс, быстрее) | "single"
    video_format_mode: str = "720_1080"    # "720" (приоритет 720p) | "720_1080"
    probe_max_reprobe: int = 1             # максимум свежих re-probe после ошибки media_url
    video_definition_filter: bool = True   # отсекать definition=="sd" до probe (API)

    # --- Precheck / AI-farm fingerprint (дешёвый, без MiMo) ---
    farm_title_weight: float = 0.35       # сходство заголовков
    farm_desc_weight: float = 0.20        # сходство описаний
    farm_duration_weight: float = 0.15    # регулярность длительности
    farm_upload_weight: float = 0.10      # регулярность загрузок
    farm_thumbnail_weight: float = 0.20   # сходство миниатюр
    farm_thumbnails_enabled: bool = False # качать миниатюры (сеть); по умолч. выкл
    farm_thumbnail_sample: int = 10       # сколько миниатюр брать для сравнения
    order_farm_threshold: float = 0.5     # выше — «ферма», порядок risky-first
    quick_accept_confidence: float = 0.80 # выше — очевидный PASS, DEEP не нужен
    farm_high_threshold: float = 0.6      # band HIGH
    farm_very_high_threshold: float = 0.8 # band VERY_HIGH
    target_region: str = ""               # регион для regionRestriction ("" = не фильтровать)
    rediscovery_queue_threshold: int = 50 # ниже этого кол-ва usable-кандидатов -> re-discovery
    rediscovery_cooldown: float = 300.0   # пауза, если re-discovery дал 0 новых (анти busy-loop)

    # --- Discovery ---
    discovery_workers: int = 4       # параллельных yt-dlp-поисков
    max_discovery_workers: int = 6   # максимум (adaptive concurrency)
    results_per_query: int = 50      # ytsearchN
    benchmark_results: list[int] = field(default_factory=lambda: [30, 50, 75])
    exploit_ratio: float = 0.70      # доля best-запросов в цикле
    explore_ratio: float = 0.20      # доля новых/экспериментальных
    random_ratio: float = 0.10       # доля случайных
    enable_query_scoring: bool = True
    enable_dynamic_query_generation: bool = False
    dynamic_query_max_per_candidate: int = 5
    discovery_min_duration: float = 5.0  # нижняя граница длительности discovery (>5 с по ТЗ)
    discovery_queries_per_run: int = 240      # сколько query выбирать на прогон (микс пулов)
    base_query_ratio: float = 0.70            # доля BASE в миксе
    expansion_query_ratio: float = 0.20       # доля EXPANSION
    experiment_query_ratio: float = 0.10      # доля EXPERIMENT
    min_query_runs_before_adaptation: int = 2 # запусков до включения adaptive priority

    # --- Приоритет низких просмотров (bands) ---
    low_view_threshold_1: int = 1000
    low_view_threshold_2: int = 3000
    low_view_threshold_3: int = 10000

    # --- Two-stage MiMo ---
    deep_confidence_threshold: float = 0.7  # ниже — неуверенно -> MiMo DEEP

    # --- Технический анализ статичности ---
    static_min_duration: float = 30 * 60.0
    static_sample_frames: int = 12
    static_motion_threshold: float = 0.02
    static_scale_width: int = 200

    # --- MiMo ---
    mimo_max_tokens: int = 2048
    mimo_temperature: float = 0.1
    mimo_use_response_format: bool = False
    mimo_enable_thinking: bool = False
    mimo_retries: int = 3
    mimo_min_confidence: float = 0.5

    # --- DeepSeek (vision через Messages API /v1/messages) ---
    # По умолчанию выключен: оценщик кадров остаётся MiMo.
    deepseek_enabled: bool = False
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://llm.azati.ai"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout: float = 180.0
    deepseek_max_tokens: int = 4096   # 2048 иногда обрезает reasoning → пустой JSON
    deepseek_retries: int = 3

    # --- Локальный VLM pre-filter (экономия вызовов MiMo) ---
    # В штатном режиме выключен: не меняет существующее поведение.
    local_vlm_enabled: bool = False
    local_vlm_mode: str = "shadow"          # "shadow" (сравнивать, не влиять) | "active" (auto-reject по allowlist) | "off"
    local_vlm_base_url: str = "http://127.0.0.1:8080/v1"
    local_vlm_model: str = "smolvlm2-2.2b"
    local_vlm_api_key: str = ""             # llama.cpp OpenAI-сервер обычно без ключа
    local_vlm_timeout: float = 90.0         # LOCAL_VLM_TIMEOUT_SECONDS
    local_vlm_max_concurrency: int = 1      # только 1 inference одновременно (4 ГБ VRAM)
    local_vlm_max_image_side: int = 512     # уменьшать кадр до этой стороны (экономия VRAM/времени)
    local_vlm_max_output_tokens: int = 400  # JSON с evidence обрезался на 200 — подняли до 400
    # Признаки, для которых активный режим может авто-reject. Пусто = авто-reject выключен.
    local_vlm_auto_reject_flags: list[str] = field(default_factory=list)

    # --- Категории (порядок из category_mapper) ---
    categories: list[str] = field(default_factory=categories_list)

    # --- TagMe ---
    tagme_timeout_ms: int = 15000
    tagme_ready_timeout_ms: int = 30000
    tagme_send_timeout_ms: int = 30000

    queries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def resolved_path(self, raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = self.project_root / p
        return p


def _load_queries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"Ошибка парсинга {path}: {e}") from e
    if not isinstance(data, dict):
        return []
    queries = data.get("queries", [])
    return [q for q in queries if isinstance(q, dict) and q.get("query")]


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")

    s = Settings(
        youtube_api_key=(os.getenv("YOUTUBE_API_KEY", "") or "").strip(),
        mimo_api_key=(os.getenv("MIMO_API_KEY", "") or "").strip(),
        mimo_base_url=(os.getenv("MIMO_BASE_URL", Settings.mimo_base_url) or Settings.mimo_base_url).strip(),
        mimo_model=(os.getenv("MIMO_MODEL", Settings.mimo_model) or Settings.mimo_model).strip(),
        mimo_timeout=_env_float("MIMO_TIMEOUT", 90.0),
        tagme_url=(os.getenv("TAGME_URL", Settings.tagme_url) or Settings.tagme_url).strip(),
        ffmpeg_path=(os.getenv("FFMPEG_PATH", "ffmpeg") or "ffmpeg").strip(),
        ffprobe_path=(os.getenv("FFPROBE_PATH", "ffprobe") or "ffprobe").strip(),
        playwright_profile_dir=os.getenv("PLAYWRIGHT_PROFILE_DIR", "./browser_profile"),
        logs_dir=os.getenv("LOGS_DIR", "./logs"),
        temp_dir=os.getenv("TEMP_DIR", "./temp"),
        search_queries_path=os.getenv("SEARCH_QUERIES_PATH", "./config/search_queries.yaml"),
        dry_run=_env_bool("DRY_RUN", True),
        headless=_env_bool("HEADLESS", False),
        slow_mo_ms=_env_int("SLOW_MO_MS", 0),
        youtube_quota_budget=_env_int("YOUTUBE_QUOTA_BUDGET", 9000),
        max_channels_to_process=_env_int("MAX_CHANNELS", 10),
        max_search_pages_per_query=_env_int("MAX_SEARCH_PAGES", 1),
        default_search_max_results=_env_int("SEARCH_MAX_RESULTS", 30),
        min_channel_videos=_env_int("MIN_CHANNEL_VIDEOS", 10),
        min_suitable_ratio=_env_float("MIN_SUITABLE_RATIO", 0.30),
        min_required_checked=_env_int("MIN_REQUIRED_CHECKED", 3),
        max_videos_per_channel=_env_int("MAX_VIDEOS_PER_CHANNEL", 20),
        max_shorts_ratio=_env_float("MAX_SHORTS_RATIO", 0.0),
        max_subscribers=_env_int("MAX_SUBSCRIBERS", 0),
        max_views=_env_int("MAX_VIEWS", 0),
        min_duration_seconds=_env_float("MIN_DURATION_SECONDS", 5.0),
        max_duration_seconds=_env_float("MAX_DURATION_SECONDS", 4 * 3600.0),
        min_height=_env_int("MIN_HEIGHT", 720),
        shorts_max_duration=_env_float("SHORTS_MAX_DURATION", 180.0),
        frame_count=_env_int("FRAME_COUNT", 3),
        frame_fractions=_env_float_list("FRAME_FRACTIONS", [0.10, 0.50, 0.90]),
        frame_fallback_fractions=_env_float_list("FRAME_FALLBACK_FRACTIONS", [0.30, 0.70]),
        frame_format=(os.getenv("FRAME_FORMAT", "jpg") or "jpg").strip(),
        frame_quality=_env_int("FRAME_QUALITY", 3),
        frame_max_width=_env_int("FRAME_MAX_WIDTH", 1280),
        short_video_threshold=_env_float("SHORT_VIDEO_THRESHOLD", 30.0),
        ytdlp_delay=_env_float("YTDLP_DELAY", 1.0),
        pot_server_dir=os.getenv("POT_SERVER_DIR", ""),
        cookies_file=os.getenv("COOKIES_FILE", ""),
        eval_parallel=_env_int("EVAL_PARALLEL", 4),
        bot_block_cooldown=_env_float("BOT_BLOCK_COOLDOWN", 1800.0),
        bot_block_backoff_factor=_env_float("BOT_BLOCK_BACKOFF_FACTOR", 2.0),
        bot_block_max_cooldown=_env_float("BOT_BLOCK_MAX_COOLDOWN", 7200.0),
        video_403_cooldown=_env_float("VIDEO_403_COOLDOWN", 600.0),
        frame_extraction_mode=(os.getenv("FRAME_EXTRACTION_MODE", "single") or "single").strip(),
        video_format_mode=(os.getenv("VIDEO_FORMAT_MODE", "720_1080") or "720_1080").strip(),
        probe_max_reprobe=_env_int("PROBE_MAX_REPROBE", 1),
        video_definition_filter=_env_bool("VIDEO_DEFINITION_FILTER", True),
        farm_title_weight=_env_float("FARM_TITLE_WEIGHT", 0.35),
        farm_desc_weight=_env_float("FARM_DESC_WEIGHT", 0.20),
        farm_duration_weight=_env_float("FARM_DURATION_WEIGHT", 0.15),
        farm_upload_weight=_env_float("FARM_UPLOAD_WEIGHT", 0.10),
        farm_thumbnail_weight=_env_float("FARM_THUMBNAIL_WEIGHT", 0.20),
        farm_thumbnails_enabled=_env_bool("FARM_THUMBNAILS_ENABLED", False),
        farm_thumbnail_sample=_env_int("FARM_THUMBNAIL_SAMPLE", 10),
        order_farm_threshold=_env_float("ORDER_FARM_THRESHOLD", 0.5),
        quick_accept_confidence=_env_float("QUICK_ACCEPT_CONFIDENCE", 0.80),
        farm_high_threshold=_env_float("FARM_HIGH_THRESHOLD", 0.6),
        farm_very_high_threshold=_env_float("FARM_VERY_HIGH_THRESHOLD", 0.8),
        target_region=(os.getenv("TARGET_REGION", "") or "").strip().upper(),
        rediscovery_queue_threshold=_env_int("REDISCOVERY_QUEUE_THRESHOLD", 50),
        rediscovery_cooldown=_env_float("REDISCOVERY_COOLDOWN", 300.0),
        discovery_workers=_env_int("DISCOVERY_WORKERS", 4),
        max_discovery_workers=_env_int("MAX_DISCOVERY_WORKERS", 6),
        results_per_query=_env_int("RESULTS_PER_QUERY", 50),
        exploit_ratio=_env_float("EXPLOIT_RATIO", 0.70),
        explore_ratio=_env_float("EXPLORE_RATIO", 0.20),
        random_ratio=_env_float("RANDOM_RATIO", 0.10),
        enable_query_scoring=_env_bool("ENABLE_QUERY_SCORING", True),
        enable_dynamic_query_generation=_env_bool("ENABLE_DYNAMIC_QUERY_GENERATION", False),
        dynamic_query_max_per_candidate=_env_int("DYNAMIC_QUERY_MAX_PER_CANDIDATE", 5),
        discovery_min_duration=_env_float("DISCOVERY_MIN_DURATION", 5.0),
        discovery_queries_per_run=_env_int("DISCOVERY_QUERIES_PER_RUN", 240),
        base_query_ratio=_env_float("BASE_QUERY_RATIO", 0.70),
        expansion_query_ratio=_env_float("EXPANSION_QUERY_RATIO", 0.20),
        experiment_query_ratio=_env_float("EXPERIMENT_QUERY_RATIO", 0.10),
        min_query_runs_before_adaptation=_env_int("MIN_QUERY_RUNS_BEFORE_ADAPTATION", 2),
        low_view_threshold_1=_env_int("LOW_VIEW_THRESHOLD_1", 1000),
        low_view_threshold_2=_env_int("LOW_VIEW_THRESHOLD_2", 3000),
        low_view_threshold_3=_env_int("LOW_VIEW_THRESHOLD_3", 10000),
        deep_confidence_threshold=_env_float("DEEP_CONFIDENCE_THRESHOLD", 0.7),
        static_min_duration=_env_float("STATIC_MIN_DURATION", 30 * 60.0),
        static_sample_frames=_env_int("STATIC_SAMPLE_FRAMES", 12),
        static_motion_threshold=_env_float("STATIC_MOTION_THRESHOLD", 0.02),
        static_scale_width=_env_int("STATIC_SCALE_WIDTH", 200),
        mimo_max_tokens=_env_int("MIMO_MAX_TOKENS", 2048),
        mimo_temperature=_env_float("MIMO_TEMPERATURE", 0.1),
        mimo_use_response_format=_env_bool("MIMO_USE_RESPONSE_FORMAT", False),
        mimo_enable_thinking=_env_bool("MIMO_ENABLE_THINKING", False),
        mimo_retries=_env_int("MIMO_RETRIES", 3),
        mimo_min_confidence=_env_float("MIMO_MIN_CONFIDENCE", 0.5),
        deepseek_enabled=_env_bool("DEEPSEEK_ENABLED", False),
        deepseek_api_key=(os.getenv("DEEPSEEK_API_KEY", "") or "").strip(),
        deepseek_base_url=(os.getenv("DEEPSEEK_BASE_URL", Settings.deepseek_base_url) or Settings.deepseek_base_url).strip(),
        deepseek_model=(os.getenv("DEEPSEEK_MODEL", Settings.deepseek_model) or Settings.deepseek_model).strip(),
        deepseek_timeout=_env_float("DEEPSEEK_TIMEOUT", 180.0),
        deepseek_max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 4096),
        deepseek_retries=_env_int("DEEPSEEK_RETRIES", 3),
        local_vlm_enabled=_env_bool("LOCAL_VLM_ENABLED", False),
        local_vlm_mode=(os.getenv("LOCAL_VLM_MODE", "shadow") or "shadow").strip().lower(),
        local_vlm_base_url=(os.getenv("LOCAL_VLM_BASE_URL", Settings.local_vlm_base_url) or Settings.local_vlm_base_url).strip(),
        local_vlm_model=(os.getenv("LOCAL_VLM_MODEL", Settings.local_vlm_model) or Settings.local_vlm_model).strip(),
        local_vlm_api_key=(os.getenv("LOCAL_VLM_API_KEY", "") or "").strip(),
        local_vlm_timeout=_env_float("LOCAL_VLM_TIMEOUT_SECONDS", 90.0),
        local_vlm_max_concurrency=_env_int("LOCAL_VLM_MAX_CONCURRENCY", 1),
        local_vlm_max_image_side=_env_int("LOCAL_VLM_MAX_IMAGE_SIDE", 512),
        local_vlm_max_output_tokens=_env_int("LOCAL_VLM_MAX_OUTPUT_TOKENS", 400),
        local_vlm_auto_reject_flags=_env_str_list("LOCAL_VLM_AUTO_REJECT_FLAGS", []),
        tagme_timeout_ms=_env_int("TAGME_TIMEOUT_MS", 15000),
        tagme_ready_timeout_ms=_env_int("TAGME_READY_TIMEOUT_MS", 30000),
        tagme_send_timeout_ms=_env_int("TAGME_SEND_TIMEOUT_MS", 30000),
    )

    # Приводим пути к абсолютным.
    s.temp_dir = str(s.resolved_path(s.temp_dir))
    s.logs_dir = str(s.resolved_path(s.logs_dir))
    s.playwright_profile_dir = str(s.resolved_path(s.playwright_profile_dir))
    s.search_queries_path = str(s.resolved_path(s.search_queries_path))

    s.queries = _load_queries(Path(s.search_queries_path))
    return s
