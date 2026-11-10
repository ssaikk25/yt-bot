# -*- coding: utf-8 -*-
"""Тесты запрета форматов: скринкасты, подкасты, моушн-дизайн."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.category_mapper import categories_list
from app.channel_evaluator import HARD_ARTIFACT_FLAGS
from app.mimo_client import build_system_prompt, to_frame_analysis
from app.precheck import is_hard_junk_title


class TestPromptForbidsFormats(unittest.TestCase):
    def setUp(self):
        self.prompt = build_system_prompt(categories_list())

    def test_prompt_mentions_screencast(self):
        self.assertIn("screen_recording", self.prompt)
        self.assertIn("скринкаст", self.prompt.lower())

    def test_prompt_mentions_podcast(self):
        self.assertIn("podcast", self.prompt)
        self.assertIn("подкаст", self.prompt.lower())

    def test_prompt_mentions_motion_design(self):
        self.assertIn("motion_design", self.prompt)
        self.assertIn("моушн", self.prompt.lower())

    def test_prompt_marks_them_unsuitable(self):
        low = self.prompt.lower()
        self.assertIn("жёстко запрещённые форматы", low)
        self.assertIn("suitable=false", low)


class TestFlagsParsedAndEnforced(unittest.TestCase):
    def test_flags_parsed(self):
        parsed = {"suitable": True, "category": "Наука и технологии",
                  "screen_recording": True, "podcast": True, "motion_design": True}
        fa = to_frame_analysis(parsed)
        self.assertTrue(fa.flags["screen_recording"])
        self.assertTrue(fa.flags["podcast"])
        self.assertTrue(fa.flags["motion_design"])

    def test_flags_are_hard(self):
        for f in ("screen_recording", "podcast", "motion_design"):
            self.assertIn(f, HARD_ARTIFACT_FLAGS, f)


class TestTitlePreFilter(unittest.TestCase):
    def test_podcast_title_rejected(self):
        self.assertTrue(is_hard_junk_title("Мой подкаст выпуск 5"))
        self.assertTrue(is_hard_junk_title("The Daily Podcast Episode 12"))

    def test_screencast_title_rejected(self):
        self.assertTrue(is_hard_junk_title("Скринкаст настройки роутера"))
        self.assertTrue(is_hard_junk_title("Screen recording of my desktop"))

    def test_motion_design_title_rejected(self):
        self.assertTrue(is_hard_junk_title("Motion design intro pack"))
        self.assertTrue(is_hard_junk_title("Моушн дизайн заставка"))

    def test_normal_title_kept(self):
        self.assertFalse(is_hard_junk_title("Токарный станок в работе"))
        self.assertFalse(is_hard_junk_title("Весенняя обрезка винограда"))


if __name__ == "__main__":
    unittest.main()
