# -*- coding: utf-8 -*-
"""Unit-тесты чистых правил отбора (без сети)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import (
    candidate_score,
    decide_30_percent,
    median_views,
    query_score,
    required_suitable,
)
from app.query_pool import QueryPool, normalize_query


class TestThirtyPercent(unittest.TestCase):
    def test_required(self):
        self.assertEqual(required_suitable(10), 3)   # ceil(3.0)
        self.assertEqual(required_suitable(11), 4)   # ceil(3.3)
        self.assertEqual(required_suitable(15), 5)   # ceil(4.5)
        self.assertEqual(required_suitable(20), 6)
        self.assertEqual(required_suitable(100), 30)
        self.assertEqual(required_suitable(2), 1)    # минимум 1

    def test_early_success(self):
        # 20 видео, required=6, уже 6 PASS -> PASS
        self.assertEqual(decide_30_percent(6, 6, 20), "PASS")

    def test_early_impossible_fail(self):
        # 20 видео, required=6, проверено 15, passed=0, remaining=5 -> 0+5 < 6 -> FAIL
        self.assertEqual(decide_30_percent(0, 15, 20), "FAIL")

    def test_not_fail_when_still_possible(self):
        # passed=1, checked=15, remaining=5 -> 1+5=6 >= 6 -> CONTINUE (не FAIL)
        self.assertEqual(decide_30_percent(1, 15, 20), "CONTINUE")

    def test_continue_after_few_checks(self):
        # passed=1, checked=3, remaining=17 -> 1+17 >= 6 -> CONTINUE
        self.assertEqual(decide_30_percent(1, 3, 20), "CONTINUE")


class TestScoring(unittest.TestCase):
    def test_median_not_skewed_by_viral(self):
        self.assertEqual(median_views([100, 200, 300]), 200)
        self.assertEqual(median_views([100, 200, 500000]), 200)

    def test_viral_video_does_not_make_channel_popular(self):
        small = candidate_score(100, [100, 200, 300])
        viral = candidate_score(100, [100, 200, 500000])
        self.assertAlmostEqual(small, viral)  # медиана одинакова

    def test_smaller_channel_higher_priority(self):
        # меньше подписчиков -> меньше score -> выше приоритет
        self.assertLess(candidate_score(10, [50]), candidate_score(10000, [50]))

    def test_query_score_range(self):
        s = query_score(0.5, 0.5, 0.5, 0.5, 0.5)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_query_score_new_dominates(self):
        # больше новых каналов -> выше score
        a = query_score(0.9, 0.0)
        b = query_score(0.1, 0.0)
        self.assertGreater(a, b)


class TestNormalization(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_query("  Lathe   TURNING Metal "), "lathe turning metal")
        self.assertEqual(normalize_query("Токарный   Станок"), "токарный станок")

    def test_pool_dedup(self):
        pool = QueryPool([
            {"query": "lathe turning", "language": "en"},
            {"query": "  Lathe   TURNING ", "language": "en"},  # дубль после нормализации
            {"query": "toczenie metalu", "language": "pl"},
        ])
        self.assertEqual(pool.size(), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
