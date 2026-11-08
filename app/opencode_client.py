"""Vision-РєР»РёРµРЅС‚ С‡РµСЂРµР· Р»РѕРєР°Р»СЊРЅС‹Р№ СЃРµСЂРІРµСЂ opencode (`opencode serve`).

РСЃРїРѕР»СЊР·СѓРµС‚ Р±РµСЃРїР»Р°С‚РЅСѓСЋ РјРѕРґРµР»СЊ opencode/deepseek-v4.1-flash-free: РєР°РґСЂС‹ СѓС…РѕРґСЏС‚
РєР°Рє file-С‡Р°СЃС‚Рё (data-URL) РІ POST /session/:id/message. РЎРёСЃС‚РµРјРЅС‹Р№ РїСЂРѕРјРїС‚ Рё
РїР°СЂСЃРµСЂ РѕС‚РІРµС‚Р° С‚Рµ Р¶Рµ, С‡С‚Рѕ Сѓ MiMoClient, РїРѕСЌС‚РѕРјСѓ РІРµСЂРґРёРєС‚С‹ СЃРѕРїРѕСЃС‚Р°РІРёРјС‹, Р°
РёРЅС‚РµСЂС„РµР№СЃ (analyze/snapshot/reset_counters) вЂ” drop-in Р·Р°РјРµРЅР°.

РўСЂРµР±СѓРµС‚ Р·Р°РїСѓС‰РµРЅРЅС‹Р№ `opencode serve` (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ http://127.0.0.1:4097).
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any, Optional

import httpx

from .access_control import mimo_note_rate_limit, mimo_note_success
from .logger import get_logger
from .mimo_client import _extract_json, build_system_prompt, to_frame_analysis
from .models import FrameAnalysis

log = get_logger("opencode")


class OpenCodeVisionClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4097",
        provider_id: str = "opencode",
        model: str = "deepseek-v4.1-flash-free",
        categories: Optional[list[str]] = None,
        timeout: float = 180.0,
        retries: int = 3,
        keep_sessions: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.provider_id = provider_id
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.keep_sessions = keep_sessions
        self.system_prompt = build_system_prompt(categories or [])
        self._lock = threading.Lock()
        self.calls = 0
        self.call_errors = 0

    def reset_counters(self) -> None:
        with self._lock:
            self.calls = 0
            self.call_errors = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"calls": self.calls, "call_errors": self.call_errors}

    @staticmethod
    def _guess_mime(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def _image_part(self, data: bytes, index: int) -> dict[str, Any]:
        mime = self._guess_mime(data)
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "file",
            "mime": mime,
            "filename": f"frame_{index:02d}.jpg",
            "url": f"data:{mime};base64,{b64}",
        }

    def analyze(self, images: list[bytes]) -> FrameAnalysis:
        """РђРЅР°Р»РёР·РёСЂСѓРµС‚ РєР°РґСЂС‹ РѕРґРЅРѕРіРѕ РІРёРґРµРѕ (С„РѕСЂРјР°С‚ РѕС‚РІРµС‚Р° РєР°Рє Сѓ MiMo)."""
        if not images:
            raise ValueError("РќРµС‚ РёР·РѕР±СЂР°Р¶РµРЅРёР№ РґР»СЏ Р°РЅР°Р»РёР·Р°")

        parts: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"Р’РѕС‚ {len(images)} РєР°РґСЂРѕРІ РёР· РѕРґРЅРѕРіРѕ РІРёРґРµРѕ (РІ РїРѕСЂСЏРґРєРµ РІСЂРµРјРµРЅРё). "
                "РќРµ РІС‹Р·С‹РІР°Р№ РёРЅСЃС‚СЂСѓРјРµРЅС‚С‹. РџСЂРѕР°РЅР°Р»РёР·РёСЂСѓР№ Рё РІРµСЂРЅРё СЃС‚СЂРѕРіРёР№ JSON."
            ),
        }]
        parts.extend(self._image_part(img, i) for i, img in enumerate(images))

        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                text = self._post(parts)
                parsed = _extract_json(text)
                with self._lock:
                    self.calls += 1
                mimo_note_success()
                return to_frame_analysis(parsed)
            except Exception as e:
                last_error = e
                log.warning("opencode РїРѕРїС‹С‚РєР° %d/%d: %s", attempt + 1, self.retries, str(e)[:300])
                if "429" in str(e):
                    mimo_note_rate_limit()
                    time.sleep(15 * (attempt + 1))
                elif attempt < self.retries - 1:
                    time.sleep(2 ** attempt * 1.0)
        with self._lock:
            self.call_errors += 1
        raise RuntimeError(
            f"opencode-РјРѕРґРµР»СЊ РЅРµ РѕС‚РІРµС‚РёР»Р° РїРѕСЃР»Рµ {self.retries} РїРѕРїС‹С‚РѕРє: {last_error}"
        )

    def _post(self, parts: list[dict[str, Any]]) -> str:
        with httpx.Client(timeout=self.timeout) as client:
            session = client.post(f"{self.base_url}/session", json={"title": "vision"})
            session.raise_for_status()
            session_id = session.json()["id"]
            try:
                resp = client.post(
                    f"{self.base_url}/session/{session_id}/message",
                    json={
                        "model": {"providerID": self.provider_id, "modelID": self.model},
                        "system": self.system_prompt,
                        "parts": parts,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            finally:
                if not self.keep_sessions:
                    try:
                        client.delete(f"{self.base_url}/session/{session_id}", timeout=10.0)
                    except Exception:
                        pass

        blocks = data.get("parts") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise RuntimeError(f"РќРµРѕР¶РёРґР°РЅРЅР°СЏ СЃС‚СЂСѓРєС‚СѓСЂР° РѕС‚РІРµС‚Р° opencode: {str(data)[:300]}")
        text = "\n".join(
            p.get("text", "") for p in blocks
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        if not text:
            kinds = [p.get("type") for p in blocks if isinstance(p, dict)]
            raise RuntimeError(f"РџСѓСЃС‚РѕР№ РѕС‚РІРµС‚ opencode (С‡Р°СЃС‚Рё: {kinds})")
        return text
