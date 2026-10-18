# -*- coding: utf-8 -*-
"""Тесты новой pipeline-логики: API pre-filter, denominator, early-stop."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.access_control import reset_mimo_parallel
from app.channel_evaluator import ChannelEvaluator
from app.config import Settings
from app.models import ChannelCandidate, FrameAnalysis, VideoCandidate, VideoEvaluation
from app.rules import decide_30_percent, required_suitable
from app.video_selector import _access_ok


def vc(vid: str, title: str = "t", dur: float = 60.0) -> VideoCandidate:
    return VideoCandidate(video_id=vid, channel_id="UC1", channel_title="c",
                         title=title, duration_seconds=dur)


def ve(video: VideoCandidate, suitable: bool = False, error_kind: str = "",
       temp_error: bool = False) -> VideoEvaluation:
    analysis = None
    if suitable:
        analysis = FrameAnalysis(suitable=True, category="Физика", description="",
                                 confidence=0.9, reason="", flags={})
    return VideoEvaluation(video=video, suitable=suitable,
                           error_kind=error_kind, temp_error=temp_error,
                           analysis=analysis)


class _FakeMimo:
    def __init__(self):
        self.calls = 0
        self.errors = 0

    def snapshot(self):
        return {"calls": self.calls, "call_errors": self.errors}

    def analyze(self, images):
        self.calls += 1
        from app.models import FrameAnalysis
        return FrameAnalysis(suitable=False, category="", description="",
                             confidence=0.0, reason="", flags={})

    def reset_counters(self):
        pass


class TestAccessFilter(unittest.TestCase):
    def test_age_restricted_rejected(self):
        ok, reason = _access_ok({"contentRating": {"ytRating": "ytAgeRestricted"}}, "")
        self.assertFalse(ok)
        self.assertEqual(reason, "age_restricted")

    def test_region_blocked(self):
        content = {"regionRestriction": {"blocked": ["RU"]}}
        self.assertFalse(_access_ok(content, "RU")[0])
        self.assertTrue(_access_ok(content, "US")[0])   # RU заблокирован, но цель US

    def test_region_allowed(self):
        content = {"regionRestriction": {"allowed": ["US", "CA"]}}
        self.assertTrue(_access_ok(content, "US")[0])
        self.assertFalse(_access_ok(content, "RU")[0])  # RU нет в allowed

    def test_no_region_filter_without_target(self):
        content = {"regionRestriction": {"allowed": ["US"]}}
        self.assertTrue(_access_ok(content, "")[0])     # без target региона не фильтруем

    def test_clean_video_ok(self):
        self.assertTrue(_access_ok({}, "")[0])


class TestDenominator(unittest.TestCase):
    def test_required_after_unavailable(self):
        # 15 найдено, 5 unavailable -> 10 eligible -> required 3
        self.assertEqual(required_suitable(15 - 5, 0.30), 3)

    def test_pass_math(self):
        self.assertEqual(decide_30_percent(3, 3, 10, 0.30), "PASS")

    def test_fail_math(self):
        # 15 eligible, required 5, passed 1, checked 12 -> remaining 3 -> 1+3<5 -> FAIL
        self.assertEqual(decide_30_percent(1, 12, 15, 0.30), "FAIL")

    def test_continue_math(self):
        self.assertEqual(decide_30_percent(1, 3, 10, 0.30), "CONTINUE")


class TestEvaluatorEarlyStop(unittest.TestCase):
    def setUp(self):
        # последовательный режим (parallel=1) для детерминизма
        reset_mimo_parallel(base=1, min_par=1)

    def _make(self, results, parallel=1):
        settings = Settings(min_suitable_ratio=0.30, max_videos_per_channel=15)
        mimo = _FakeMimo()
        evaluator = ChannelEvaluator(settings, None, mimo, None, parallel=parallel)
        calls = {"n": 0}

        def fake_eval(v):
            i = calls["n"]
            calls["n"] += 1
            return results[min(i, len(results) - 1)]

        evaluator._evaluate_video = fake_eval
        return evaluator, calls

    def test_early_pass_stops_calling(self):
        results = [ve(vc(f"v{i}"), suitable=True) for i in range(3)] + \
                  [ve(vc(f"v{i}")) for i in range(3, 10)]
        evaluator, calls = self._make(results)
        ev = evaluator.evaluate(ChannelCandidate(channel_id="UC1", title="c"),
                                [r.video for r in results])
        self.assertTrue(ev.passed)
        # early PASS: не должно проверить все 10, только 3 (required=3)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(ev.checked, 3)

    def test_early_fail_stops_calling(self):
        results = [ve(vc(f"v{i}")) for i in range(15)]  # все unsuitable
        evaluator, calls = self._make(results)
        ev = evaluator.evaluate(ChannelCandidate(channel_id="UC1", title="c"),
                                [r.video for r in results])
        self.assertFalse(ev.passed)
        # FAIL при checked=11 (15-5+1), не 15
        self.assertEqual(calls["n"], 11)
        self.assertEqual(ev.checked, 11)

    def test_unavailable_excluded_from_denominator(self):
        results = [ve(vc(f"u{i}"), error_kind="unavailable") for i in range(5)] + \
                  [ve(vc(f"v{i}")) for i in range(10)]
        evaluator, calls = self._make(results)
        ev = evaluator.evaluate(ChannelCandidate(channel_id="UC1", title="c"),
                                [r.video for r in results])
        self.assertEqual(ev.unavailable_count, 5)
        # denominator = 10 (не 15) -> required=3 -> FAIL при checked=8, а не 11
        self.assertEqual(ev.checked, 8)
        self.assertFalse(ev.passed)

    def test_smaller_than_parallel_no_refill_spin(self):
        # регрессия: 2 видео при parallel=4 раньше крутило бесконечный refill
        reset_mimo_parallel(base=4, min_par=4)
        results = [ve(vc(f"v{i}")) for i in range(2)]  # оба unsuitable
        evaluator, calls = self._make(results, parallel=4)
        ev = evaluator.evaluate(ChannelCandidate(channel_id="UC1", title="c"),
                                [r.video for r in results])
        self.assertFalse(ev.passed)
        self.assertEqual(calls["n"], 2)  # оба проверены, без зависания


if __name__ == "__main__":
    unittest.main(verbosity=2)
