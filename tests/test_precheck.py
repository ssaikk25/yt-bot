# -*- coding: utf-8 -*-
"""Unit-тесты дешёвого pre-MiMo анализа (farm fingerprint / ordering / title risk)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.precheck import (
    compute_farm_risk,
    dominant_template_ratio,
    duration_regularity,
    is_hard_junk_title,
    mean_pairwise_jaccard,
    normalize_text,
    order_videos,
    title_risk,
    title_to_template,
    upload_regularity,
)


class TestNormalization(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_text("  LATHE   Turning  Metal "), "lathe turning metal")

    def test_unicode_nfkc(self):
        self.assertEqual(normalize_text("Ťěžký"), "ťěžký")

    def test_template_digits(self):
        self.assertEqual(title_to_template("Geschichten vom Land – Folge 183"),
                         "geschichten vom land folge #")
        self.assertEqual(title_to_template("Geschichten vom Land – Folge 182"),
                         "geschichten vom land folge #")


class TestTemplate(unittest.TestCase):
    def test_dominant_ratio_high(self):
        titles = [f"AI Story Folge {i}" for i in range(180, 190)]
        count, ratio, _ = dominant_template_ratio(titles)
        self.assertEqual(count, len(titles))
        self.assertGreater(ratio, 0.9)

    def test_dominant_ratio_low(self):
        titles = ["lathe turning", "tractor plowing", "beehive inspection",
                  "wood carving", "pottery wheel", "blacksmith forging"]
        _, ratio, _ = dominant_template_ratio(titles)
        self.assertLess(ratio, 0.5)


class TestSimilarity(unittest.TestCase):
    def test_identical_titles(self):
        self.assertAlmostEqual(mean_pairwise_jaccard(["same title", "same title", "same title"]), 1.0)

    def test_distinct_titles(self):
        self.assertLess(mean_pairwise_jaccard(["lathe turning metal", "tractor plowing field", "beehive"]), 0.4)


class TestRegularity(unittest.TestCase):
    def test_duration_uniform_low_cv(self):
        _, _, _, cv = duration_regularity([480, 481, 480, 482, 480])
        self.assertLess(cv, 0.01)

    def test_duration_varied_high_cv(self):
        _, _, _, cv = duration_regularity([60, 300, 1200, 45, 900])
        self.assertGreater(cv, 0.5)

    def test_upload_regular(self):
        times = ["2025-01-01T00:00:00Z", "2025-01-01T06:00:00Z",
                 "2025-01-01T12:00:00Z", "2025-01-01T18:00:00Z"]
        cv, score = upload_regularity(times)
        self.assertLess(cv, 0.01)
        self.assertGreater(score, 0.9)


class TestTitleRisk(unittest.TestCase):
    def test_ai_story_high(self):
        score, hits = title_risk("AI Story of a boy who fixed tractors")
        self.assertGreater(score, 0.9)
        self.assertIn("ai_content", hits)

    def test_clean_title_zero(self):
        score, _ = title_risk("tractor plowing field in the village")
        self.assertEqual(score, 0.0)

    def test_hard_junk_true(self):
        self.assertTrue(is_hard_junk_title("Police Car Driving Simulator gameplay"))
        self.assertTrue(is_hard_junk_title("White noise for sleep"))

    def test_hard_junk_no_false_positive(self):
        # слово «game» в составе другого слова не должно ловиться
        self.assertFalse(is_hard_junk_title("metalworking machine maintenance"))
        self.assertFalse(is_hard_junk_title("the game of thrones review") is True)  # нет «game» в списке
        self.assertFalse(is_hard_junk_title("tractor plowing field"))


class TestFarmRisk(unittest.TestCase):
    def test_farm_like_high(self):
        titles = [f"AI Story Folge {i}" for i in range(15)]
        descs = ["clickbait generated story"] * 15
        durations = [480.0] * 15
        published = ["2025-01-01T00:00:00Z"] * 15
        risk = compute_farm_risk(titles, descs, durations, published)
        self.assertGreater(risk, 0.6)

    def test_real_channel_low(self):
        titles = ["lathe turning small part", "tractor plowing field", "beehive inspection",
                  "wood carving", "pottery wheel", "blacksmith forging", "hydraulic press"]
        descs = ["workshop demo", "spring field work", "apiary check",
                 "carving a spoon", "making a vase", "forging a knife", "press test"]
        durations = [180, 540, 900, 240, 300, 420, 150]
        published = ["2025-01-01T00:00:00Z", "2025-02-03T00:00:00Z", "2025-03-10T00:00:00Z",
                     "2025-04-20T00:00:00Z", "2025-06-01T00:00:00Z", "2025-07-15T00:00:00Z",
                     "2025-09-01T00:00:00Z"]
        risk = compute_farm_risk(titles, descs, durations, published)
        self.assertLess(risk, 0.4)


class TestOrdering(unittest.TestCase):
    def _mk(self, title):
        class V:
            def __init__(self, t):
                self.title = t
        return V(title)

    def test_farm_risky_first(self):
        videos = [self._mk("clean lathe turning"), self._mk("AI Story generated"),
                  self._mk("tractor plowing"), self._mk("Top 10 compilation")]
        ordered = order_videos(videos, farm_risk=0.8, threshold=0.5)
        self.assertEqual(ordered[0].title, "AI Story generated")

    def test_normal_clean_first(self):
        videos = [self._mk("AI Story generated"), self._mk("clean lathe turning")]
        ordered = order_videos(videos, farm_risk=0.1, threshold=0.5)
        self.assertEqual(ordered[0].title, "clean lathe turning")


if __name__ == "__main__":
    unittest.main(verbosity=2)
