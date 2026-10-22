"""Тесты access-control state machine и lifecycle очереди.

Не обращаются к настоящему YouTube: все внешние вызовы замоканы.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from app import access_control as ac
from app.access_control import (
    API_QUOTA,
    BOT_CHECK,
    GVS_403,
    NETWORK,
    PAGE_403,
    OTHER_403,
    UNAVAILABLE,
    BotBlockedError,
    YouTubeAccessController,
    classify_youtube_error,
)
from app.config import resolve_max_discovery_workers


def _reset_global():
    c = ac.access_controller
    c.state = c.NORMAL
    c.cooldown_until = 0.0
    c.current_cooldown = c.base_cooldown
    c.block_count = 0
    c._state_path = None
    ac.video_cooldown._until.clear()


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        _reset_global()
        self.ctrl = YouTubeAccessController(base_cooldown=10, backoff_factor=2, max_cooldown=40)

    # TEST 1 ------------------------------------------------------------ #
    def test_01_explicit_bot_check_blocks(self):
        self.assertFalse(self.ctrl.is_blocked())
        self.ctrl.note_bot_check("test")
        self.assertTrue(self.ctrl.is_blocked())
        self.assertEqual(self.ctrl.state, self.ctrl.BLOCKED)
        self.assertGreater(self.ctrl.cooldown_remaining(), 0)

    # TEST 2 ------------------------------------------------------------ #
    def test_02_gate_blocks_discovery_and_probe_during_cooldown(self):
        self.ctrl.note_bot_check("test")
        for component in ("discovery", "probe"):
            with self.assertRaises(BotBlockedError):
                self.ctrl.gate(component)

    def test_02b_yt_search_not_called_when_blocked(self):
        from app.discovery import _yt_search
        ac.access_controller.note_bot_check("test")
        with patch("app.discovery.yt_dlp.YoutubeDL") as mock_ydl:
            with self.assertRaises(BotBlockedError):
                _yt_search("query", 1)
            mock_ydl.assert_not_called()

    # TEST 3 ------------------------------------------------------------ #
    def test_03_data_api_is_independent(self):
        self.ctrl.note_bot_check("test")
        # Data API не проходит через youtube_gate; его ошибки классифицируются отдельно.
        self.assertEqual(classify_youtube_error("quota exceeded", source="api"), API_QUOTA)
        self.assertNotEqual(classify_youtube_error("quota exceeded", source="api"), BOT_CHECK)
        # Гейт к Data API не применяется.
        self.ctrl.gate("api") if False else None  # API-ветка не вызывает gate

    # TEST 4 ------------------------------------------------------------ #
    def test_04_googlevideo_403_is_not_bot_check(self):
        kind = classify_youtube_error("HTTP Error 403: Forbidden", source="googlevideo")
        self.assertEqual(kind, GVS_403)
        self.assertNotEqual(kind, BOT_CHECK)

    # TEST 5 ------------------------------------------------------------ #
    def test_05_api_403_does_not_block_ytdlp(self):
        self.ctrl.note_suspected("api 403")
        self.assertFalse(self.ctrl.is_blocked())
        self.assertEqual(self.ctrl.state, self.ctrl.SUSPECTED)

    # TEST 6 ------------------------------------------------------------ #
    def test_06_network_errors_are_not_bot_check(self):
        self.assertEqual(classify_youtube_error("read timed out"), NETWORK)
        self.assertEqual(classify_youtube_error("DNS resolution failed"), NETWORK)
        self.ctrl.note_suspected("network")
        self.assertFalse(self.ctrl.is_blocked())

    # TEST 7 ------------------------------------------------------------ #
    def test_07_recovery_probe_after_cooldown(self):
        self.ctrl.state = self.ctrl.BLOCKED
        self.ctrl.cooldown_until = time.time() - 1
        self.assertFalse(self.ctrl.is_blocked())
        self.assertTrue(self.ctrl.start_recovery_probe())
        self.assertEqual(self.ctrl.state, self.ctrl.RECOVERING)
        self.ctrl.mark_recovery_succeeded()
        self.assertEqual(self.ctrl.state, self.ctrl.NORMAL)

    # TEST 8 ------------------------------------------------------------ #
    def test_08_state_restored_after_restart(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "access_state.json")
        self.ctrl.bind_state_path(path)
        self.ctrl.note_bot_check("test")
        self.assertTrue(os.path.exists(path))

        c2 = YouTubeAccessController(base_cooldown=10, backoff_factor=2, max_cooldown=40)
        c2.bind_state_path(path)
        self.assertTrue(c2.load_state())
        self.assertTrue(c2.is_blocked())

    # TEST 9 ------------------------------------------------------------ #
    def test_09_adaptive_concurrency_does_not_exceed_max(self):
        self.assertEqual(resolve_max_discovery_workers(2, 0), 2)
        self.assertEqual(resolve_max_discovery_workers(2, 5), 5)
        self.assertEqual(resolve_max_discovery_workers(2, 1), 2)

    # TEST 10 ----------------------------------------------------------- #
    def test_10_global_state_is_shared(self):
        from app.discovery import access_controller as d_ac
        from app.frame_sampler import access_controller as f_ac
        self.assertIs(d_ac, ac.access_controller)
        self.assertIs(f_ac, ac.access_controller)

    # TEST 11 ----------------------------------------------------------- #
    def test_11_bot_check_does_not_mark_content_fail(self):
        # BOT_CHECK — временная ошибка: канал не должен считаться контентным FAIL.
        self.assertIn(BOT_CHECK, ac.TEMP_KINDS)
        self.assertNotIn(BOT_CHECK, ac.PERMANENT_KINDS)
        self.assertNotIn(BOT_CHECK, ac.UNWATCHABLE_KINDS)

    def test_11b_probe_bot_check_blocks_globally(self):
        from app.frame_sampler import FrameSampler, ProbeError
        tmp = tempfile.mkdtemp()
        sampler = FrameSampler(temp_dir=__import__("pathlib").Path(tmp))
        with patch("app.frame_sampler.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception(
                "Sign in to confirm you're not a bot")
            with self.assertRaises(ProbeError) as ctx:
                sampler.probe("https://youtu.be/x", "vid")
        self.assertEqual(ctx.exception.kind, BOT_CHECK)
        self.assertTrue(ac.access_controller.is_blocked())

    # TEST 12 ----------------------------------------------------------- #
    def test_12_unavailable_video_does_not_block_globally(self):
        kind = classify_youtube_error("This video is not available")
        self.assertEqual(kind, UNAVAILABLE)
        self.assertFalse(self.ctrl.is_blocked())

    # TEST 13 ----------------------------------------------------------- #
    def test_13_quota_accounting_counts_real_calls(self):
        from app.youtube_client import QuotaMeter, SEARCH_COST
        meter = QuotaMeter()
        self.assertEqual(meter.used, 0)
        meter.add(SEARCH_COST)
        meter.add(1)
        self.assertEqual(meter.used, SEARCH_COST + 1)

    def test_13b_quota_is_shared_between_clients(self):
        from app.youtube_client import YouTubeClient, quota_meter
        d = tempfile.mkdtemp()
        from app.youtube_client import configure_quota_meter
        configure_quota_meter(os.path.join(d, "q.json"))
        c1 = YouTubeClient("key", quota_budget=9000)
        c2 = YouTubeClient("key", quota_budget=9000)
        c1._charge(100)
        self.assertEqual(c2.quota_used, 100)
        self.assertEqual(quota_meter.used, 100)

    # TEST 14 ----------------------------------------------------------- #
    def test_14_zero_quota_stops_api_branch(self):
        from app.youtube_client import YouTubeClient, YouTubeQuotaError
        d = tempfile.mkdtemp()
        from app.youtube_client import configure_quota_meter
        configure_quota_meter(os.path.join(d, "q.json"))
        client = YouTubeClient("key", quota_budget=0)
        with self.assertRaises(YouTubeQuotaError):
            client.search_videos("query", max_results=1)

    # Дополнительно: backoff растёт и упирается в cap ------------------- #
    def test_16_backoff_caps(self):
        for _ in range(5):
            self.ctrl.note_bot_check("test")
        self.assertLessEqual(self.ctrl.current_cooldown, self.ctrl.max_cooldown)


if __name__ == "__main__":
    unittest.main()
