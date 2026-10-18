# -*- coding: utf-8 -*-
"""Unit-тесты доступа к YouTube: классификация ошибок, state machine, cooldown, expire."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.access_control import (
    AGE_GATE,
    BOT_CHECK,
    FORBIDDEN,
    NETWORK,
    UNAVAILABLE,
    UNKNOWN,
    UNSUPPORTED,
    URL_EXPIRED,
    TEMP_KINDS,
    PERMANENT_KINDS,
    VideoErrorCooldown,
    YouTubeAccessController,
    classify_youtube_error,
)
from app.models import VideoMeta


class TestClassification(unittest.TestCase):
    def test_bot_check(self):
        self.assertEqual(classify_youtube_error("Sign in to confirm you're not a bot"), BOT_CHECK)

    def test_bot_check_short(self):
        self.assertEqual(classify_youtube_error("Please confirm you're not a bot"), BOT_CHECK)

    def test_age_gate(self):
        self.assertEqual(classify_youtube_error("Sign in to confirm your age"), AGE_GATE)

    def test_unavailable(self):
        self.assertEqual(classify_youtube_error("This video is not available"), UNAVAILABLE)

    def test_removed(self):
        self.assertEqual(classify_youtube_error("This video has been removed by the uploader"), UNAVAILABLE)

    def test_forbidden_403(self):
        self.assertEqual(classify_youtube_error("HTTP Error 403: Forbidden"), FORBIDDEN)

    def test_url_expired(self):
        self.assertEqual(classify_youtube_error("The signature has expired"), URL_EXPIRED)

    def test_network_timeout(self):
        self.assertEqual(classify_youtube_error("connection timed out"), NETWORK)

    def test_unsupported_format(self):
        self.assertEqual(classify_youtube_error("Requested format is not available"), UNSUPPORTED)

    def test_unknown(self):
        self.assertEqual(classify_youtube_error(""), UNKNOWN)

    def test_temp_vs_permanent(self):
        self.assertIn(BOT_CHECK, TEMP_KINDS)
        self.assertIn(FORBIDDEN, TEMP_KINDS)
        self.assertIn(URL_EXPIRED, TEMP_KINDS)
        self.assertIn(NETWORK, TEMP_KINDS)
        self.assertIn(UNAVAILABLE, PERMANENT_KINDS)
        self.assertIn(AGE_GATE, PERMANENT_KINDS)


class TestStateMachine(unittest.TestCase):
    def test_backoff_increases_cooldown(self):
        c = YouTubeAccessController(base_cooldown=100.0, backoff_factor=2.0, max_cooldown=400.0)
        c.note_bot_check()
        self.assertEqual(c.state, c.BLOCKED)
        self.assertEqual(c.current_cooldown, 100.0)
        c.note_bot_check()
        self.assertEqual(c.current_cooldown, 200.0)
        c.note_bot_check()
        self.assertEqual(c.current_cooldown, 400.0)  # cap
        c.note_bot_check()
        self.assertEqual(c.current_cooldown, 400.0)  # stays capped

    def test_success_resets(self):
        c = YouTubeAccessController(base_cooldown=100.0)
        c.note_bot_check()
        c.note_bot_check()
        self.assertEqual(c.current_cooldown, 200.0)
        c.note_success()
        self.assertEqual(c.state, c.NORMAL)
        self.assertEqual(c.current_cooldown, 100.0)
        self.assertFalse(c.is_blocked())

    def test_blocked_while_cooldown(self):
        c = YouTubeAccessController(base_cooldown=3600.0)
        c.note_bot_check()
        self.assertTrue(c.is_blocked())

    def test_suspected_transition(self):
        c = YouTubeAccessController(base_cooldown=100.0)
        c.note_suspected()
        self.assertEqual(c.state, c.SUSPECTED)
        c.note_suspected()  # повторный suspected не меняет
        self.assertEqual(c.state, c.SUSPECTED)


class TestVideoCooldown(unittest.TestCase):
    def test_mark_and_block(self):
        vc = VideoErrorCooldown(cooldown_sec=600.0)
        vc.mark("vid1")
        self.assertTrue(vc.is_blocked("vid1"))
        self.assertFalse(vc.is_blocked("vid2"))

    def test_clear(self):
        vc = VideoErrorCooldown(cooldown_sec=600.0)
        vc.mark("vid1")
        vc.clear("vid1")
        self.assertFalse(vc.is_blocked("vid1"))


class TestVideoMetaExpiry(unittest.TestCase):
    def test_not_expired_unknown(self):
        m = VideoMeta(video_id="v", media_url_expire=0.0)
        self.assertFalse(m.media_url_expired)

    def test_not_expired_future(self):
        m = VideoMeta(video_id="v", media_url_expire=time.time() + 1000)
        self.assertFalse(m.media_url_expired)

    def test_expired_past(self):
        m = VideoMeta(video_id="v", media_url_expire=time.time() - 1000)
        self.assertTrue(m.media_url_expired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
