"""Оценка канала по правилу минимум 30% подходящих видео.

Логика ранней остановки:
- Если подходящих уже достаточно для прохождения порога И проверено не менее
  требуемого минимума — канал проходит, дальше не проверяем.
- Если даже при условии, что все оставшиеся видео подходят, порог недостижим —
  канал отбрасывается, дальше не проверяем.
- Видео с временной ошибкой (bot-check/403/expire/network) НЕ считаются решёнными:
  не портят 30%-правило и не помечают канал отклонённым.
"""
from __future__ import annotations

import math
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

from .access_control import (
    TEMP_KINDS,
    UNWATCHABLE_KINDS,
    access_metrics,
    adjust_mimo_parallel,
    current_mimo_parallel,
    init_mimo_parallel,
    mimo_is_rate_limited,
)
from .category_mapper import CATEGORY_KEY_MAP, canonicalize
from .frame_sampler import FrameSampler, MediaUrlError, ProbeError, compute_timestamps
from .local_vlm import LocalVlmClient
from .logger import get_logger
from .mimo_client import MiMoClient
from .models import ChannelCandidate, ChannelEvaluation, VideoCandidate, VideoEvaluation
from .precheck import compute_farm_risk, farm_risk_band, order_videos, video_priority
from .rules import decide_30_percent, required_suitable
from .thumbhash import thumbnails_similarity
from .video_checks import check_technical, image_dark_fraction

log = get_logger("evaluator")

# Артефакты, при наличии которых видео считается неподходящим.
HARD_ARTIFACT_FLAGS = [
    "watermark", "borders", "overlay_text", "strong_shake", "collage",
    "blur", "pixelation", "ai_generated", "too_dark", "talking_head",
    "unclear_content", "slideshow",
]
# speed_change НЕ входит в жёсткие: по отдельным кадрам его нельзя достоверно
# определить; о нём только логируем.


class ChannelEvaluator:
    def __init__(self, settings, youtube, mimo: MiMoClient, sampler: FrameSampler, parallel: int = 4):
        self.settings = settings
        self.youtube = youtube
        self.mimo = mimo
        self.sampler = sampler
        self.parallel = max(1, parallel)
        init_mimo_parallel(parallel)
        # Локальный VLM pre-filter (опционально; LOCAL_VLM_ENABLED=false -> None, поведение не меняется).
        self.local_vlm = LocalVlmClient.from_settings(settings) if settings.local_vlm_enabled else None

    def evaluate(self, channel: ChannelCandidate, pool: list[VideoCandidate]) -> ChannelEvaluation:
        ev = ChannelEvaluation(channel=channel)
        if not pool:
            ev.reason = "пул пуст (нет подходящих видео)"
            return ev

        self.parallel = current_mimo_parallel()
        snap0 = self.mimo.snapshot()

        eligible = pool[: self.settings.max_videos_per_channel]
        ev.eligible_count = len(eligible)  # total_found (до probe)

        # --- Дешёвый channel fingerprint (без MiMo) + умный порядок проверки ---
        farm_risk = self._compute_farm_risk(channel, eligible)
        ev.farm_risk = farm_risk
        order_scores = {v.video_id: video_priority(v.title, farm_risk) for v in eligible}
        eligible = order_videos(eligible, farm_risk, self.settings.order_farm_threshold)

        log.info(
            "Канал %s: farm_risk=%.2f (%s) | videos_total=%d",
            channel.title, farm_risk,
            farm_risk_band(farm_risk, self.settings.farm_high_threshold,
                           self.settings.farm_very_high_threshold),
            len(eligible),
        )

        # --- Динамическая подача задач: решение проверяется ПОСЛЕ КАЖДОГО видео ---
        unavailable_count = 0
        temp_count = 0
        decided = False
        executor = ThreadPoolExecutor(max_workers=self.parallel)
        pending: dict = {}
        video_iter = iter(eligible)

        def submit_next() -> bool:
            v = next(video_iter, None)
            if v is None:
                return False
            pending[executor.submit(self._evaluate_video, v)] = v
            return True

        for _ in range(self.parallel):
            if not submit_next():
                break

        try:
            while pending and not decided:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    vv = fut.result()
                    del pending[fut]
                    ev.evaluations.append(vv)

                    if vv.error_kind in UNWATCHABLE_KINDS:
                        # «Не просматриваемое» (unavailable/age): не PASS, не FAIL, вне denominator.
                        unavailable_count += 1
                        log.info("Канал %s: видео %s -> UNAVAILABLE [%s] (вне denominator)",
                                 channel.title, vv.video.video_id, vv.error_kind)
                        continue
                    if vv.temp_error:
                        ev.temp_error = True
                        temp_count += 1
                        log.info("Канал %s: видео %s -> TEMP [%s]",
                                 channel.title, vv.video.video_id, vv.error_kind or "?")
                        continue

                    ev.checked += 1
                    if vv.suitable:
                        ev.suitable += 1
                    log.info(
                        "Канал %s: видео %s (%.2f) (%s) -> %s | %s",
                        channel.title, vv.video.video_id,
                        order_scores.get(vv.video.video_id, 0.0), vv.video.title[:60],
                        "OK" if vv.suitable else "NO", vv.reject_reason or "-",
                    )

                    # denominator = реально проверяемые (минус unavailable). Проверяем после КАЖДОГО видео.
                    total = len(eligible) - unavailable_count
                    required = required_suitable(total, self.settings.min_suitable_ratio)
                    decision = decide_30_percent(ev.suitable, ev.checked, total, self.settings.min_suitable_ratio)
                    if decision == "PASS":
                        decided = True
                        ev.passed = True
                        ev.reason = f"PASS: passed={ev.suitable} >= required={required} (checked={ev.checked}/{total})"
                        break
                    if decision == "FAIL":
                        decided = True
                        ev.passed = False
                        remaining = total - ev.checked
                        ev.reason = f"FAIL: passed={ev.suitable} remaining={remaining} < required={required}"
                        break

                # refill: новые задачи запускаем ТОЛЬКО если решение ещё не доказано
                if not decided:
                    while len(pending) < self.parallel and submit_next():
                        pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not ev.reason:
            total = len(eligible) - unavailable_count
            required = required_suitable(total, self.settings.min_suitable_ratio)
            if temp_count > 0 and ev.checked < total:
                ev.passed = False
                ev.reason = f"не решено: {total - ev.checked} видео дали temp-ошибку"
            else:
                ratio = ev.suitable / max(1, total)
                ev.passed = ev.suitable >= required
                ev.reason = f"итог: подходят {ev.suitable}/{total} = {ratio:.0%}"

        ev.unavailable_count = unavailable_count

        # Прозрачность знаменателя 30%.
        total = len(eligible) - unavailable_count
        required = required_suitable(total, self.settings.min_suitable_ratio)
        remaining = max(0, total - ev.checked)
        verdict = "PASS" if ev.passed else ("TEMP" if ev.temp_error else "FAIL")
        log.info(
            "Канал %s: ИТОГ videos_total=%d unavailable=%d eligible=%d required=%d "
            "checked=%d passed=%d failed=%d remaining=%d -> %s",
            channel.title, len(eligible), unavailable_count, total, required,
            ev.checked, ev.suitable, ev.checked - ev.suitable, remaining,
            verdict,
        )

        if ev.passed:
            ev.best_category, ev.best_category_key = self._best_category(ev)
            if not ev.best_category:
                ev.passed = False
                ev.reason = "не удалось выбрать категорию"

        # Adaptive MiMo concurrency: ошибки -> снижаем, стабильно -> возвращаем.
        snap1 = self.mimo.snapshot()
        new_par = adjust_mimo_parallel(
            snap1["call_errors"] - snap0["call_errors"],
            snap1["calls"] - snap0["calls"],
        )
        if new_par != self.parallel:
            log.info("MiMo parallel: %d -> %d (calls=%d errors=%d)",
                     self.parallel, new_par,
                     snap1["calls"] - snap0["calls"],
                     snap1["call_errors"] - snap0["call_errors"])
        return ev

    def _compute_farm_risk(self, channel: ChannelCandidate, eligible: list[VideoCandidate]) -> float:
        """Дешёвый channel-level AI-farm fingerprint (без MiMo)."""
        try:
            titles = [v.title for v in eligible]
            descs = [v.description for v in eligible]
            durations = [v.duration_seconds for v in eligible]
            published = [v.published_at for v in eligible]
            thumb_sim = None
            if self.settings.farm_thumbnails_enabled:
                thumb_sim, _ = thumbnails_similarity(
                    eligible, Path(self.settings.temp_dir) / "thumbnails",
                    sample=self.settings.farm_thumbnail_sample,
                )
            return compute_farm_risk(
                titles, descs, durations, published,
                w_title=self.settings.farm_title_weight,
                w_desc=self.settings.farm_desc_weight,
                w_duration=self.settings.farm_duration_weight,
                w_upload=self.settings.farm_upload_weight,
                w_thumbnail=self.settings.farm_thumbnail_weight,
                thumbnail_similarity=thumb_sim,
            )
        except Exception as e:
            log.warning("farm_risk для %s: %s", channel.title, str(e)[:120])
            return 0.0

    def _needs_deep(self, analysis) -> tuple[bool, str]:
        """Решает, нужен ли MiMo DEEP. DEEP — только при реальной неуверенности."""
        if not analysis.suitable:
            return False, "очевидный FAIL"
        if analysis.confidence >= self.settings.quick_accept_confidence:
            return False, f"уверенный PASS conf={analysis.confidence:.2f}"
        if analysis.confidence < self.settings.deep_confidence_threshold:
            return True, f"низкая уверенность conf={analysis.confidence:.2f}"
        return False, f"приемлемая уверенность conf={analysis.confidence:.2f}"

    def _evaluate_video(self, v: VideoCandidate) -> VideoEvaluation:
        vv = VideoEvaluation(video=v)

        # --- probe (ОДИН на видео) ---
        try:
            meta = self.sampler.probe(v.url, v.video_id, self.settings.min_height)
        except ProbeError as e:
            vv.reject_reason = f"probe [{e.kind}]: {str(e)[:160]}"
            vv.error_kind = e.kind
            if e.kind in TEMP_KINDS:
                vv.temp_error = True
            log.warning("probe %s: [%s] %s", v.video_id, e.kind, str(e)[:140])
            return vv
        except Exception as e:
            vv.reject_reason = f"probe: {str(e)[:160]}"
            log.warning("probe %s: %s", v.video_id, e)
            return vv

        tech = check_technical(self.settings, meta)
        vv.technical = tech
        if not tech.ok:
            vv.reject_reason = tech.error
            return vv

        timestamps = compute_timestamps(
            meta.duration,
            self.settings.frame_count,
            self.settings.frame_fractions,
            self.settings.short_video_threshold,
        )

        # --- кадры (с одним свежим re-probe при 403/expire) ---
        try:
            frames = self._extract_with_reprobe(meta, timestamps, v)
        except MediaUrlError as e:
            vv.reject_reason = f"media_url [{e.kind}]: {str(e)[:160]}"
            vv.error_kind = e.kind
            if e.kind in TEMP_KINDS:
                vv.temp_error = True
                self.sampler.mark_video_temp_error(v.video_id)
            log.warning("frames %s: [%s] %s", v.video_id, e.kind, str(e)[:140])
            return vv

        if len(frames) < 2:
            vv.reject_reason = "не удалось извлечь достаточно кадров"
            return vv

        # Техническая проверка темноты (независимо от MiMo).
        dark_frac = sum(image_dark_fraction(p) for p in frames) / len(frames)

        try:
            images = [p.read_bytes() for p in frames]
        except OSError as e:
            vv.reject_reason = f"ошибка чтения кадров: {e}"
            return vv

        # --- Локальный VLM pre-filter (опционально; shadow/active) ---
        local_result = None
        if self.local_vlm is not None:
            labels = [f"{int(round(f * 100))}" for f in self.settings.frame_fractions[:len(frames)]]
            local_result = self.local_vlm.analyze_frames(
                list(zip(labels, images)),
                {"video_id": v.video_id, "title": v.title, "duration": meta.duration,
                 "channel_id": v.channel_id},
                target_category=None,
            )
            vv.local_vlm = local_result
            # Активный режим: заменяем вызов MiMo только по allowlist-признакам.
            # Локальная модель НИКОГДА не ставит PASS — она может только reject.
            if self.local_vlm.is_active and local_result.is_reject and \
                    local_result.auto_reject_triggered(self.local_vlm.auto_reject_flags):
                vv.reject_reason = f"local_vlm: {local_result.reason or ', '.join(local_result.flags)}"
                self.local_vlm.tracker.record(
                    local_result, mimo_suitable=None, mimo_flags={},
                    mimo_category="", mimo_confidence=0.0, mimo_error=False,
                    mimo_skipped=True,
                    context={"video_id": v.video_id, "channel_id": v.channel_id,
                             "title": v.title, "target_category": None},
                )
                log.info("local_vlm %s: авто-отказ по [%s], MiMo пропущен",
                         v.video_id, ", ".join(local_result.flags))
                return vv

        def _record_local(mimo_suitable=None, mimo_flags=None, mimo_category="",
                          mimo_confidence=0.0, mimo_error=False):
            if local_result is not None:
                self.local_vlm.tracker.record(
                    local_result, mimo_suitable=mimo_suitable,
                    mimo_flags=mimo_flags or {}, mimo_category=mimo_category,
                    mimo_confidence=mimo_confidence, mimo_error=mimo_error,
                    context={"video_id": v.video_id, "channel_id": v.channel_id,
                             "title": v.title, "target_category": None},
                )

        # --- MiMo QUICK (3 кадра: начало/середина/конец) ---
        if mimo_is_rate_limited():
            vv.error_kind = "mimo_429"
            vv.temp_error = True
            vv.reject_reason = "MiMo rate-limited (429), отложено"
            _record_local(mimo_error=True)
            return vv
        try:
            analysis = self.mimo.analyze(images)
        except Exception as e:
            # MiMo-сбой = нет вердикта = временная ошибка (retry), НЕ permanent reject.
            vv.error_kind = "mimo_error"
            vv.temp_error = True
            vv.reject_reason = f"ошибка MiMo (temp): {str(e)[:160]}"
            log.warning("mimo %s (temp): %s", v.video_id, e)
            _record_local(mimo_error=True)
            return vv

        if dark_frac > 0.5:
            analysis.flags["too_dark"] = True
            if "too_dark" in analysis.raw:
                analysis.raw["too_dark"] = True

        # --- Two-stage: QUICK -> DEEP только когда вердикт реально неуверенный ---
        need_deep, deep_reason = self._needs_deep(analysis)
        if need_deep:
            fallback = self.settings.frame_fallback_fractions
            if fallback:
                deep_ts = compute_timestamps(
                    meta.duration, len(fallback), fallback, self.settings.short_video_threshold
                )
                try:
                    deep_frames = self.sampler.extract_frames(
                        meta, deep_ts, prefix=v.video_id + "_deep",
                        quality=self.settings.frame_quality, fmt=self.settings.frame_format,
                        max_width=self.settings.frame_max_width,
                    )
                except MediaUrlError as e:
                    log.warning("MiMo DEEP %s: media_url [%s], пропускаю DEEP", v.video_id, e.kind)
                    deep_frames = []
                if deep_frames:
                    try:
                        deep_images = [p.read_bytes() for p in deep_frames]
                        analysis = self.mimo.analyze(images + deep_images)
                        if dark_frac > 0.5:
                            analysis.flags["too_dark"] = True
                        log.info("MiMo DEEP %s: conf=%.2f cat=%r", v.video_id, analysis.confidence, analysis.category)
                    except Exception as e:
                        log.warning("MiMo DEEP %s: %s", v.video_id, str(e)[:120])
        else:
            log.info("MiMo %s: DEEP не нужен (%s)", v.video_id, deep_reason)

        vv.analysis = analysis

        if analysis.flags.get("speed_change"):
            log.info("MiMo отметил speed_change у %s (информативно, не жёсткий отказ)", v.video_id)

        if not analysis.suitable:
            vv.reject_reason = f"MiMo: unsuitable ({analysis.reason[:150]})"
        else:
            bad = [k for k in HARD_ARTIFACT_FLAGS if analysis.flags.get(k)]
            if bad:
                vv.reject_reason = f"артефакты: {', '.join(bad)}"
            else:
                cat = canonicalize(analysis.category)
                if not cat:
                    vv.reject_reason = f"категория не распознана: {analysis.category!r}"
                elif analysis.confidence < self.settings.mimo_min_confidence:
                    vv.reject_reason = f"низкая уверенность {analysis.confidence:.2f}"
                else:
                    vv.suitable = True

        # shadow-сравнение: локальный вердикт vs итог MiMo (не влияет на решение).
        _record_local(mimo_suitable=vv.suitable, mimo_flags=analysis.flags,
                      mimo_category=analysis.category, mimo_confidence=analysis.confidence)
        return vv

    def _extract_with_reprobe(self, meta, timestamps, v) -> list:
        """Извлечение кадров с максимум одним свежим re-probe при 403/expire/network."""
        max_reprobe = max(0, self.settings.probe_max_reprobe)
        try:
            return self.sampler.extract_frames(
                meta, timestamps, prefix=v.video_id,
                quality=self.settings.frame_quality, fmt=self.settings.frame_format,
                max_width=self.settings.frame_max_width,
            )
        except MediaUrlError as e:
            if e.kind not in TEMP_KINDS or max_reprobe <= 0:
                raise
            access_metrics.record_reprobe()
            log.info("re-probe %s после [%s]", v.video_id, e.kind)
            meta2 = self.sampler.probe(v.url, v.video_id, self.settings.min_height)
            return self.sampler.extract_frames(
                meta2, timestamps, prefix=v.video_id,
                quality=self.settings.frame_quality, fmt=self.settings.frame_format,
                max_width=self.settings.frame_max_width,
            )

    def _best_category(self, ev: ChannelEvaluation) -> tuple[str, str]:
        votes: dict[str, list[float]] = {}
        for vv in ev.evaluations:
            if vv.suitable and vv.analysis:
                cat = canonicalize(vv.analysis.category)
                if cat:
                    votes.setdefault(cat, []).append(vv.analysis.confidence)

        if not votes:
            return "", ""

        def score(item: tuple[str, list[float]]) -> tuple[int, float]:
            cat, confs = item
            return (len(confs), sum(confs))

        best = max(votes.items(), key=score)
        key = CATEGORY_KEY_MAP.get(best[0], "")
        return best[0], key
