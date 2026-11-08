# -*- coding: utf-8 -*-
"""Тесты усиленной проверки качества: минимальная выборка, пограничная зона,
несмещённый порядок видео, минимальный пул."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.precheck import order_videos
from app.rules import decide_30_percent, required_suitable


class TestDecide30Percent(unittest.TestCase):
    def test_pass_requires_min_checked(self):
        # порог достигнут (3 из 10), но выборка меньше минимума -> CONTINUE
        self.assertEqual(decide_30_percent(3, 3, 10, 0.30, min_checked=10), "CONTINUE")

    def test_pass_when_enough_checks(self):
        self.assertEqual(decide_30_percent(10, 10, 10, 0.30, min_checked=10), "PASS")

    def test_pass_when_pool_smaller_than_min_checked(self):
        # в пуле всего 3 видео -> требование не может быть больше пула
        self.assertEqual(decide_30_percent(3, 3, 3, 0.30, min_checked=10), "PASS")

    def test_borderline_continues(self):
        # 4/10 = 40% при пороге 30% и margin 0.10 -> пограничный, добираем
        self.assertEqual(
            decide_30_percent(4, 10, 20, 0.30, min_checked=10, border_margin=0.10),
            "CONTINUE")
        # 10/20 = 50% -> уверенный PASS
        self.assertEqual(
            decide_30_percent(10, 20, 20, 0.30, min_checked=10, border_margin=0.10),
            "PASS")

    def test_fail_still_early(self):
        # даже если все оставшиеся пройдут, порог недостижим:
        # 0 из 18 при пуле 20 -> осталось 2, нужно 6 -> FAIL
        self.assertEqual(decide_30_percent(0, 18, 20, 0.30, min_checked=10), "FAIL")

    def test_required_suitable(self):
        self.assertEqual(required_suitable(10, 0.30), 3)
        self.assertEqual(required_suitable(1, 0.30), 1)


class TestOrderingUnbiased(unittest.TestCase):
    def _mk(self, title, vid):
        class V:
            def __init__(self, t, i):
                self.title = t
                self.video_id = i
        return V(title, vid)

    def test_deterministic(self):
        vs = [self._mk("clean", "a"), self._mk("AI Story generated", "b"),
              self._mk("risky stuff", "q")]
        a = [v.video_id for v in order_videos(vs, 0.1, 0.5)]
        b = [v.video_id for v in order_videos(vs, 0.1, 0.5)]
        self.assertEqual(a, b)
        self.assertEqual(sorted(a), ["a", "b", "q"])

    def test_farm_keeps_risky_first(self):
        vs = [self._mk("clean lathe", "a"), self._mk("AI Story generated", "b")]
        out = order_videos(vs, farm_risk=0.9, threshold=0.5)
        self.assertEqual(out[0].video_id, "b")


class TestMinPoolSetting(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertEqual(s.min_checked_for_pass, 10)
        self.assertAlmostEqual(s.border_margin, 0.10)
        self.assertEqual(s.min_channel_videos, 10)


if __name__ == "__main__":
    unittest.main()
