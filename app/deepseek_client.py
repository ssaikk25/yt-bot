"""Клиент vision-оценщика через DeepSeek Messages API (/v1/messages).

Важно: DeepSeek-модели принимают изображения НЕ через OpenAI `image_url`
(там модель отвечает «I don't see an image»), а через **Messages API**
с блоком `{"type": "image", "source": {"type": "base64", ...}}`.
Без этого формата vision не работает.

Интерфейс повторяет `MiMoClient.analyze(images) -> FrameAnalysis`, поэтому
клиент подключается как drop-in замена через фабрику `create_vision_client(settings)`.
Использует тот же системный промпт и парсер, что и MiMo, — сравнение честное.
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Optional

import httpx

from .logger import get_logger
from .mimo_client import MiMoClient, _extract_json, build_system_prompt, to_frame_analysis
from .models import FrameAnalysis

log = get_logger("deepseek")


class DeepSeekClient:
    """Vision-оценщик через DeepSeek Messages API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://llm.azati.ai",
        model: str = "deepseek-flash",
        categories: Optional[list[str]] = None,
        timeout: float = 180.0,
        retries: int = 3,
        max_tokens: int = 2048,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.max_tokens = max_tokens
        self.system_prompt = build_system_prompt(categories or [])
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

    @staticmethod
    def _guess_mime(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def _image_block(self, data: bytes) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self._guess_mime(data),
                "data": base64.b64encode(data).decode("ascii"),
            },
        }

    def analyze(self, images: list[bytes]) -> FrameAnalysis:
        """Анализирует кадры одного видео (тот же формат ответа, что у MiMo)."""
        if not images:
            raise ValueError("Нет изображений для анализа")
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY не задан")

        content: list[dict] = [{"type": "text", "text": "Вот кадры из одного видео (в порядке времени). Проанализируй и верни строгий JSON."}]
        content.extend(self._image_block(img) for img in images)
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": [{"role": "user", "content": content}],
        }

        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                text = self._post(payload)
                parsed = _extract_json(text)
                with self._lock:
                    self.calls += 1
                return to_frame_analysis(parsed)
            except Exception as e:
                last_error = e
                log.warning("DeepSeek попытка %d/%d: %s", attempt + 1, self.retries, str(e)[:300])
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt * 1.0)
        with self._lock:
            self.call_errors += 1
        raise RuntimeError(f"DeepSeek не ответил после {self.retries} попыток: {last_error}")

    def _post(self, payload: dict) -> str:
        url = f"{self.base_url}/v1/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # Messages API: content = список блоков (thinking + text). Берём только text.
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError(f"Неожиданная структура ответа DeepSeek: {str(data)[:300]}")
        parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise RuntimeError(f"Пустой text от DeepSeek (блоки: {[b.get('type') for b in blocks if isinstance(b, dict)]})")
        return text


def create_vision_client(settings):
    """Фабрика оценщика кадров: DeepSeek, если DEEPSEEK_ENABLED=true, иначе MiMo."""
    if getattr(settings, "deepseek_enabled", False) and settings.deepseek_api_key:
        log.info("Оценщик кадров: DeepSeek (%s)", settings.deepseek_model)
        return DeepSeekClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            categories=settings.categories,
            timeout=settings.deepseek_timeout,
            retries=settings.deepseek_retries,
            max_tokens=settings.deepseek_max_tokens,
        )
    return MiMoClient(
        api_key=settings.mimo_api_key,
        base_url=settings.mimo_base_url,
        model=settings.mimo_model,
        categories=settings.categories,
        timeout=settings.mimo_timeout,
        retries=settings.mimo_retries,
        max_tokens=settings.mimo_max_tokens,
        temperature=settings.mimo_temperature,
        use_response_format=settings.mimo_use_response_format,
        enable_thinking=settings.mimo_enable_thinking,
    )
