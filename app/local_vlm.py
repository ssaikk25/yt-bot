"""Локальный vision-language pre-filter (VLM) для экономии платных вызовов MiMo.

Локальная модель (первый кандидат — SmolVLM2-2.2B-Instruct Q4_K_M через llama.cpp)
смотрит на ТЕ ЖЕ кадры, что уже извлечены `frame_sampler.py`, и выносит
консервативный вердикт об ОЧЕВИДНЫХ визуальных дефектах.

Ключевые принципы:
- работает локально через OpenAI-совместимый сервер llama.cpp (`/v1/chat/completions`);
- НЕ обращается к внешнему AI API;
- НЕ скачивает видео и НЕ делает лишних обращений к YouTube (кадры уже есть);
- в shadow-режиме (по умолчанию) НЕ влияет на PASS/FAIL — только логирует и сравнивает
  свой вердикт с вердиктом MiMo;
- в active-режиме может заменять вызов MiMo только по allowlist-признакам, и никогда
  не выставляет каналу финальный PASS;
- при любой ошибке/недоступности локальной модели видео уходит в MiMo как обычно
  (graceful fallback).
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image

from .logger import get_logger
from .models import LocalVlmResult

log = get_logger("local_vlm")

# Разрешённые имена признаков (строгий enum ответа локальной модели).
# Намеренно НЕТ ai_generated / talking_head / strong_shake / speed_change —
# их нельзя достоверно определить по отдельным кадрам, и они НЕ подлежат авто-reject.
LOCAL_VLM_FLAGS = [
    "watermark", "borders", "overlay_text", "blur", "pixelation",
    "too_dark", "collage", "slideshow", "other",
]

VERDICT_REJECT = "OBVIOUS_REJECT"
VERDICT_UNCERTAIN = "UNCERTAIN"
VERDICT_NO_REJECT = "NO_OBVIOUS_REJECT"
VERDICTS = {VERDICT_REJECT, VERDICT_UNCERTAIN, VERDICT_NO_REJECT}


def _extract_json_object(text: str) -> dict:
    """Достаёт ПЕРВЫЙ сбалансированный JSON-объект из ответа модели.

    Маленькая локальная модель (SmolVLM2-2.2B) иногда дописывает после JSON
    пояснительный текст или повторяет объект — обычный `text.find("{")..rfind("}")`
    на этом ломается ("Extra data"). Здесь парсим по балансу фигурных скобок.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start == -1:
        raise ValueError("в ответе нет JSON-объекта")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("несбалансированный JSON в ответе")


LOCAL_VLM_SYSTEM_PROMPT = (
    "You are a strict visual pre-filter for stock-style videos. "
    "You see a few frames from ONE video. Your only job is to detect OBVIOUS visual defects "
    "that would make the video clearly unsuitable. Be conservative: mark OBVIOUS_REJECT only "
    "when a defect is clearly visible in the provided frames. Base every decision on what is "
    "actually visible in a specific frame - never on the video title or on assumptions. "
    "Distinguish observed facts from guesses.\n\n"
    "Return STRICT JSON only (no markdown, no prose):\n"
    '{"verdict":"OBVIOUS_REJECT|UNCERTAIN|NO_OBVIOUS_REJECT",'
    '"flags":[],'
    '"evidence":[{"frame":"10|50|90","observation":"what is concretely visible"}],'
    '"reason":"brief"}\n\n'
    "Allowed flags (choose ONLY those you are CERTAIN you saw in the frames; usually 0-2):\n"
    "watermark, borders, overlay_text, blur, pixelation, too_dark, collage, slideshow, other\n\n"
    "Rules:\n"
    "- OBVIOUS_REJECT: at least one defect is clearly visible and dominates the frame.\n"
    "- NO_OBVIOUS_REJECT: no obvious defect visible (does NOT mean the video is perfect).\n"
    "- UNCERTAIN: unsure, or the defect is minor/ambiguous.\n"
    "- If you did not clearly see a defect, flags MUST be [] (empty). Do not list every flag.\n"
    "- use \"other\" only for a clear defect that does not fit the named categories, and describe it in evidence.\n"
    "- evidence: at most 3 entries, each observation under 15 words, referring to a specific frame.\n"
    "- reason: one short sentence.\n"
    "- never judge temporal properties (motion/shake/speed) or AI-generation from still frames."
)


def _resize_jpeg(data: bytes, max_side: int) -> bytes:
    """Уменьшает кадр так, чтобы большая сторона не превышала max_side.

    Экономит VRAM и время локального inference. Если Pillow не справится —
    возвращает исходные байты (graceful).
    """
    if max_side <= 0:
        return data
    try:
        img = Image.open(BytesIO(data)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        log.warning("local_vlm resize: %s", e)
        return data


def _image_part(data: bytes) -> dict:
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


class LocalVlmClient:
    """Клиент локальной VLM через OpenAI-совместимый сервер llama.cpp."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080/v1",
        model: str = "smolvlm2-2.2b",
        api_key: str = "",
        timeout: float = 90.0,
        max_concurrency: int = 1,
        max_image_side: int = 512,
        max_output_tokens: int = 200,
        mode: str = "shadow",
        auto_reject_flags: Optional[list[str]] = None,
        logs_dir: Path = Path("logs"),
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_image_side = max_image_side
        self.max_output_tokens = max_output_tokens
        self.mode = mode if mode in ("shadow", "active", "off") else "shadow"
        self.auto_reject_flags: set[str] = set(auto_reject_flags or [])
        # Сериализация локального inference (4 ГБ VRAM — только один поток).
        self._sem = threading.Semaphore(max(1, max_concurrency))
        self.tracker = LocalVlmTracker(logs_dir)

    @classmethod
    def from_settings(cls, settings) -> "LocalVlmClient":
        return cls(
            base_url=settings.local_vlm_base_url,
            model=settings.local_vlm_model,
            api_key=settings.local_vlm_api_key,
            timeout=settings.local_vlm_timeout,
            max_concurrency=settings.local_vlm_max_concurrency,
            max_image_side=settings.local_vlm_max_image_side,
            max_output_tokens=settings.local_vlm_max_output_tokens,
            mode=settings.local_vlm_mode,
            auto_reject_flags=settings.local_vlm_auto_reject_flags,
            logs_dir=Path(settings.logs_dir),
        )

    @property
    def is_active(self) -> bool:
        """Активный режим включён только если задан allowlist признаков."""
        return self.mode == "active" and bool(self.auto_reject_flags)

    # ------------------------------------------------------------------ #
    def analyze_frames(
        self,
        frames: list[tuple[str, bytes]],
        video_metadata: dict[str, Any],
        target_category: Optional[str] = None,
    ) -> LocalVlmResult:
        """Анализирует кадры одного видео и возвращает вердикт локальной модели.

        frames: list[(label, jpeg_bytes)], где label = "10"/"50"/"90" (позиция в %).
        video_metadata: dict с ключами video_id/title/duration (для лога).
        Никогда не бросает исключение: при ошибке возвращает LocalVlmResult(error=...).
        """
        res = LocalVlmResult()
        if not frames:
            res.error = "нет кадров"
            return res

        vid = video_metadata.get("video_id", "?")
        t0 = time.time()
        with self._sem:  # сериализуем локальный inference (VRAM)
            try:
                prepared = [(label, _resize_jpeg(data, self.max_image_side)) for label, data in frames]
                content = self._post(prepared, target_category)
                parsed = _extract_json_object(content)
                res = self._to_result(parsed)
            except Exception as e:
                res.error = str(e)[:300]
        res.latency_sec = round(time.time() - t0, 3)
        if res.error:
            log.warning("local_vlm %s: %s", vid, res.error)
        else:
            log.info("local_vlm %s: %s flags=%s (%.2fs)",
                     vid, res.verdict, ",".join(res.flags) or "-", res.latency_sec)
        return res

    def _post(self, frames: list[tuple[str, bytes]], target_category: Optional[str]) -> str:
        cat = target_category or "unknown"
        user_text = (
            f"Frames from one video (label = approximate position in percent). "
            f"Target category: {cat}. Analyze and return strict JSON."
        )
        parts: list[dict] = [{"type": "text", "text": user_text}]
        for label, data in frames:
            parts.append(_image_part(data))

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": LOCAL_VLM_SYSTEM_PROMPT},
                {"role": "user", "content": parts},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_output_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"неожиданная структура ответа llama.cpp: {str(data)[:300]}") from e
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("пустой content от llama.cpp")
        return content

    def _to_result(self, parsed: dict) -> LocalVlmResult:
        verdict = str(parsed.get("verdict", "") or "").strip().upper()
        flags_raw = parsed.get("flags") or []
        if not isinstance(flags_raw, list):
            flags_raw = [flags_raw]
        flags = [str(f).strip().lower() for f in flags_raw if str(f).strip()]
        flags = [f for f in flags if f in LOCAL_VLM_FLAGS]
        # дедупликация с сохранением порядка (маленькая модель иногда дублирует "other")
        _seen: set[str] = set()
        flags = [f for f in flags if not (f in _seen or _seen.add(f))]
        evidence_raw = parsed.get("evidence") or []
        evidence = []
        if isinstance(evidence_raw, list):
            for e in evidence_raw:
                if isinstance(e, dict) and e.get("frame"):
                    evidence.append({
                        "frame": str(e.get("frame")),
                        "observation": str(e.get("observation", "") or "")[:300],
                    })
        if verdict not in VERDICTS:
            # Непонятный вердикт = ошибка разбора (fallback в MiMo).
            raise RuntimeError(f"недопустимый verdict {verdict!r} в ответе локальной модели")
        return LocalVlmResult(
            verdict=verdict,
            flags=flags,
            evidence=evidence,
            reason=str(parsed.get("reason", "") or "")[:300],
            raw=parsed,
        )


class LocalVlmTracker:
    """Сравнительная статистика shadow-режима: локальный VLM vs MiMo.

    Пишет компактный JSONL (`local_vlm_compare.jsonl`, по строке на видео) и
    сводный JSON (`local_vlm_summary.json`) с агрегатами по вердиктам и признакам.
    """

    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.jsonl = logs_dir / "local_vlm_compare.jsonl"
        self.summary_path = logs_dir / "local_vlm_summary.json"
        self._lock = threading.Lock()
        self.stats = self._load_summary()

    # ------------------------------------------------------------------ #
    def _empty_stats(self) -> dict:
        return {
            "calls": 0,
            "errors": 0,
            "verdict_reject": 0,
            "verdict_uncertain": 0,
            "verdict_no_reject": 0,
            "latency_sum_sec": 0.0,
            "false_rejects": 0,     # локально REJECT, а MiMo признал подходящим
            "reject_matched": 0,    # локально REJECT и MiMo тоже отклонил
            "mimo_skipped": 0,      # активный режим: вызов MiMo сэкономлен
            "mimo_errors": 0,       # MiMo дал temp-ошибку (сравнить нечем)
            "flags": {},
        }

    def _load_summary(self) -> dict:
        try:
            if self.summary_path.exists():
                return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return self._empty_stats()

    def _save_summary(self) -> None:
        try:
            self.summary_path.write_text(
                json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def record(
        self,
        local: LocalVlmResult,
        mimo_suitable: Optional[bool],
        mimo_flags: dict[str, bool],
        mimo_category: str,
        mimo_confidence: float,
        mimo_error: bool,
        mimo_skipped: bool = False,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """Записывает одно сравнение (local vs MiMo)."""
        ctx = context or {}
        line = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "video_id": ctx.get("video_id", ""),
            "channel_id": ctx.get("channel_id", ""),
            "title": str(ctx.get("title", ""))[:120],
            "target_category": ctx.get("target_category", ""),
            "local": {
                "verdict": local.verdict,
                "flags": local.flags,
                "reason": local.reason,
                "error": local.error,
                "latency_sec": local.latency_sec,
            },
            "mimo": {
                "suitable": mimo_suitable,
                "flags": {k: bool(v) for k, v in (mimo_flags or {}).items() if bool(v)},
                "category": mimo_category,
                "confidence": round(float(mimo_confidence or 0.0), 3),
                "error": bool(mimo_error),
                "skipped": bool(mimo_skipped),
            },
        }
        try:
            with open(self.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("local_vlm JSONL: %s", e)

        with self._lock:
            s = self.stats
            s["calls"] += 1
            s["latency_sum_sec"] += local.latency_sec
            if local.error:
                s["errors"] += 1
            elif local.verdict == VERDICT_REJECT:
                s["verdict_reject"] += 1
            elif local.verdict == VERDICT_UNCERTAIN:
                s["verdict_uncertain"] += 1
            else:
                s["verdict_no_reject"] += 1

            if mimo_skipped:
                s["mimo_skipped"] += 1
            elif mimo_error:
                s["mimo_errors"] += 1
            elif local.is_reject and not local.error:
                # есть ground-truth MiMo для сравнения
                if mimo_suitable:
                    s["false_rejects"] += 1
                else:
                    s["reject_matched"] += 1
                for flag in local.flags:
                    f = s["flags"].setdefault(flag, {"rejects": 0, "matched": 0, "false": 0})
                    f["rejects"] += 1
                    if mimo_suitable:
                        f["false"] += 1
                    else:
                        f["matched"] += 1

            # пересчитываем precision по каждому признаку
            for flag, f in s["flags"].items():
                denom = f["matched"] + f["false"]
                f["precision"] = round(f["matched"] / denom, 3) if denom else None
            self._save_summary()

    def summary(self) -> dict:
        with self._lock:
            s = json.loads(json.dumps(self.stats))  # копия
        if s["calls"]:
            s["avg_latency_sec"] = round(s["latency_sum_sec"] / s["calls"], 3)
        return s


# Удобный экспорт для from-import'ов.
__all__ = [
    "LocalVlmClient",
    "LocalVlmTracker",
    "LOCAL_VLM_FLAGS",
    "VERDICT_REJECT",
    "VERDICT_UNCERTAIN",
    "VERDICT_NO_REJECT",
]
