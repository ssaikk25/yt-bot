"""Клиент MiMo V2.6 Flash (OpenAI-совместимый vision API).

Отправляет ТОЛЬКО отдельные изображения кадров (JPEG/PNG/WebP), закодированные
в base64 как data-URL. Целое видео не отправляется.

Формат запроса — стандартный `/chat/completions` с мультимодальным контентом.
Если ваш endpoint MiMo отличается (другой путь/поля), поправьте `_post`.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Any, Optional

import httpx

from .access_control import mimo_note_rate_limit, mimo_note_success
from .logger import get_logger
from .models import FrameAnalysis

log = get_logger("mimo")


def build_system_prompt(categories: list[str]) -> str:
    cats = "; ".join(categories)
    return f"""Ты — строгий модератор видеоконтента. Тебе даны несколько кадров из ОДНОГО видео в порядке времени.
Определи, подходит ли видео, и выбери ОДНУ категорию из списка.

Категории (только эти названия):
{cats}

Правила категоризации:
- Физика: механика, гидродинамика, оптика, термодинамика, магнетизм, добыча, металлургия, формование, механообработка, сборка, логистика, РАБОТА спецтехники (экскаватор/станок/пресс реально работает, а не просто демонстрируется), езда по бездорожью.
- Музыка: концерт, инструментальное соло, игра на инструментах, настройка/репетиция. Музыкальные клипы сюда НЕ относить.
- Дизайн: одежда, обувь, интерьер, показы мод.
- Съемки с дронов, goPRO: города, природа, архитектура, камера на теле/транспорте, любительская съёмка телефоном/видеокамерой, поезда в движении.
- Flowers: выращивание, букеты, пересадка комнатных цветов.
- Сад огород: земледелие, вспашка, посадка, сад, огород, теплица, копание/пересадка (на участке). Пересадка цветов в доме сюда НЕ относится.
- Art: картины, скульптура, гончарство, лепка, рукоделие, создание предметов искусства, украшение скетчбука.
- Кулинария: процесс приготовления еды.
- Еда: готовые блюда, продукты, рестораны, кафе, мукбанг, street food, сладости.
- Видео с активностями: деятельность человека — тренировки, уборка, танцы, гимнастика, пчеловодство, кладоискательство.
- Видео спортивные трансляции: ТОЛЬКО соревнования (матч/турнир/чемпионат/забег/лига/соперники/счёт/судейство/результат). Тренировка/разминка = Активности.
- Животные: животные/рыбы/насекомые — основной объект.
- Природа: лес, реки, водоёмы, горы и другие природные объекты.
- Наука и технологии: устройства, механизмы, роботы, электроника, Arduino, 3D-принтеры, точные науки.
- Water: вода — основной объект большую часть времени.
- Инфографика: схемы, графики, последовательная отрисовка, математическая визуализация (не приоритет).
- Высококачественные стоковые клипы: короткие качественные stock footage (не приоритет).

Спорные случаи:
- Кейпоп: много активного танца -> Видео с активностями; в основном пение и малоактивные движения -> Музыка.
- Кладоискательство: активно копает/ищет/достаёт -> Видео с активностями; обзор местности/находок -> Съемки с дронов, goPRO.

Видео НЕ подходит (suitable=false), если существенная/постоянная часть кадров содержит:
водяной знак (watermark), рамки/полосы (borders), постоянный наложенный текст (overlay_text), сильную тряску/смаз от движения (strong_shake), ожившие картины/фотографии, сильную пикселизацию (pixelation), странный обрубленный формат, коллаж (collage), полный blur, AI-сгенерированное видео (ai_generated), существенный частичный blur, слишком тёмную картинку — более 50% площади кадра в темноте (too_dark), непонятное происходящее (unclear_content), ускорение/замедление (speed_change), slideshow.
Также НЕ подходит: говорящая голова/крупный план лица как основное содержание (talking_head); влоги.
Локальные артефакты допустимы, если основная часть видео чистая.
Не делай выводов о временных характеристиках (статичность, тряска, скорость) только по отдельным кадрам — оценивай их как видимые признаки на кадрах.

Верни СТРОГО один JSON-объект, без пояснений и markdown:
{{"suitable": bool, "category": "<одна из категорий>", "watermark": bool, "borders": bool, "overlay_text": bool, "strong_shake": bool, "collage": bool, "blur": bool, "pixelation": bool, "ai_generated": bool, "too_dark": bool, "talking_head": bool, "unclear_content": bool, "slideshow": bool, "speed_change": bool, "description": "краткое описание", "confidence": 0.0, "reason": "..."}}"""


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (в т.ч. из markdown-кода)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # попытка «починить» одиночные кавычки/лишние запятые не делается намеренно —
        # лучше честно сообщить об ошибке и повторить запрос.
        raise


def _extract_json_array(text: str) -> list:
    """Достаёт JSON-МАССИВ из ответа модели (для micro-batch)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        # модель могла вернуть объект {"videos":[...]} или просто один объект
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
            return [parsed]
        return []
    except json.JSONDecodeError:
        raise


def to_frame_analysis(parsed: dict) -> FrameAnalysis:
    flag_names = [
        "watermark", "borders", "overlay_text", "strong_shake", "collage",
        "blur", "pixelation", "ai_generated", "too_dark", "talking_head",
        "unclear_content", "slideshow", "speed_change",
    ]
    flags = {k: bool(parsed.get(k, False)) for k in flag_names}
    return FrameAnalysis(
        suitable=bool(parsed.get("suitable", False)),
        category=str(parsed.get("category", "") or "").strip(),
        description=str(parsed.get("description", "") or ""),
        confidence=float(parsed.get("confidence", 0.0) or 0.0),
        reason=str(parsed.get("reason", "") or ""),
        flags=flags,
        raw=parsed,
    )


class MiMoClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        categories: list[str],
        timeout: float = 90.0,
        retries: int = 3,
        max_tokens: int = 2048,
        temperature: float = 0.1,
        use_response_format: bool = False,
        enable_thinking: bool = False,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.use_response_format = use_response_format
        # MiMo V2.6 Flash — «thinking»-модель. При включённом thinking она тратит
        # все токены на reasoning_content и не возвращает ответ, поэтому по умолчанию
        # thinking отключён (enable_thinking=false).
        self.enable_thinking = enable_thinking
        self.system_prompt = build_system_prompt(categories)
        # Счётчики вызовов для KPI (thread-safe): MiMo calls/channel.
        self._lock = threading.Lock()
        self.calls = 0
        self.call_errors = 0

    def reset_counters(self) -> None:
        with self._lock:
            self.calls = 0
            self.call_errors = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "call_errors": self.call_errors}

    def _guess_mime(self, data: bytes) -> str:
        """Определяет MIME кадра по сигнатуре, чтобы data-URL был корректным."""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def _image_part(self, data: bytes) -> dict:
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{self._guess_mime(data)};base64,{b64}"}}

    def analyze(self, images: list[bytes]) -> FrameAnalysis:
        """Анализирует кадры одного видео и возвращает вердикт."""
        if not images:
            raise ValueError("Нет изображений для анализа")
        if not self.api_key:
            raise RuntimeError("MIMO_API_KEY не задан")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Вот {len(images)} кадров из одного видео (в порядке времени). Проанализируй и верни строгий JSON."},
                        *[self._image_part(img) for img in images],
                    ],
                },
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.use_response_format:
            payload["response_format"] = {"type": "json_object"}
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking

        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                content = self._post(payload)
                parsed = _extract_json(content)
                with self._lock:
                    self.calls += 1
                mimo_note_success()
                return to_frame_analysis(parsed)
            except Exception as e:
                last_error = e
                msg = str(e)
                log.warning("MiMo попытка %d/%d: %s", attempt + 1, self.retries, msg[:300])
                # 429 = исчерпание токенов/RPM: глобальная пауза с backoff, ждём дольше.
                if "429" in msg:
                    mimo_note_rate_limit()
                    time.sleep(15 * (attempt + 1))
                elif attempt < self.retries - 1:
                    time.sleep(2 ** attempt * 1.0)
        with self._lock:
            self.call_errors += 1
        raise RuntimeError(f"MiMo не ответил после {self.retries} попыток: {last_error}")

    def analyze_multi(self, videos: list[list[bytes]]) -> list[FrameAnalysis]:
        """Микро-батч: анализирует несколько видео в ОДНОМ запросе.

        n=2 -> 1 запрос вместо 2 (если сервер обрабатывает батч почти за то же
        время, что и одиночный запрос — это почти 2x throughput).
        """
        n = len(videos)
        if n == 0:
            return []
        if n == 1:
            return [self.analyze(videos[0])]
        if not self.api_key:
            raise RuntimeError("MIMO_API_KEY не задан")

        all_images: list[bytes] = []
        groups: list[str] = []
        for i, vid in enumerate(videos):
            start = len(all_images) + 1
            end = len(all_images) + len(vid)
            groups.append(f"кадры {start}-{end} = видео {i + 1}")
            all_images.extend(vid)

        batch_prompt = self.system_prompt.replace(
            "несколько кадров из ОДНОГО видео",
            f"кадры из {n} РАЗНЫХ видео (сгруппированы)",
        ).replace(
            "Верни СТРОГО один JSON-объект, без пояснений и markdown:",
            f"Верни СТРОГО JSON-МАССИВ из {n} объектов (по одному на видео), без пояснений и markdown:",
        )

        text = (
            f"Вот {len(all_images)} кадров из {n} РАЗНЫХ видео. "
            + "; ".join(groups)
            + f". Проанализируй КАЖДОЕ видео отдельно и верни JSON-массив из {n} объектов."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": batch_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        *[self._image_part(img) for img in all_images],
                    ],
                },
            ],
            "temperature": self.temperature,
            "max_tokens": max(2048, self.max_tokens * n),
        }
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking

        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                content = self._post(payload)
                parsed = _extract_json_array(content)
                with self._lock:
                    self.calls += 1
                mimo_note_success()
                return [to_frame_analysis(p) for p in parsed]
            except Exception as e:
                last_error = e
                msg = str(e)
                log.warning("MiMo batch попытка %d/%d: %s", attempt + 1, self.retries, msg[:300])
                if "429" in msg:
                    mimo_note_rate_limit()
                    time.sleep(15 * (attempt + 1))
                elif attempt < self.retries - 1:
                    time.sleep(2 ** attempt * 1.0)
        with self._lock:
            self.call_errors += 1
        raise RuntimeError(f"MiMo batch не ответил после {self.retries} попыток: {last_error}")

    def _post(self, payload: dict) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Неожиданная структура ответа MiMo: {str(data)[:300]}") from e
        if not isinstance(content, str):
            raise RuntimeError(f"Пустой/нестроковый content MiMo: {content!r}")
        return content
