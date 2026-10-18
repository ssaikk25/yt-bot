"""Тесты локального VLM pre-filter (без сети — только парсинг/трекер/конфиг)."""
import json
import tempfile
import unittest
from pathlib import Path

from app.local_vlm import (
    LOCAL_VLM_FLAGS,
    VERDICT_NO_REJECT,
    VERDICT_REJECT,
    VERDICT_UNCERTAIN,
    LocalVlmClient,
    LocalVlmTracker,
    _resize_jpeg,
)
from app.models import LocalVlmResult


class TestToResult(unittest.TestCase):
    def setUp(self):
        self.client = LocalVlmClient(logs_dir=Path(tempfile.mkdtemp()))

    def test_valid_reject(self):
        parsed = {
            "verdict": "OBVIOUS_REJECT",
            "flags": ["watermark", "bogus_flag"],
            "evidence": [{"frame": "50", "observation": "логотип в углу"}],
            "reason": "водяной знак",
        }
        r = self.client._to_result(parsed)
        self.assertEqual(r.verdict, VERDICT_REJECT)
        # неизвестные флаги отбрасываются
        self.assertEqual(r.flags, ["watermark"])
        self.assertTrue(r.is_reject)
        self.assertEqual(len(r.evidence), 1)

    def test_valid_no_reject(self):
        r = self.client._to_result({"verdict": "NO_OBVIOUS_REJECT", "flags": []})
        self.assertEqual(r.verdict, VERDICT_NO_REJECT)
        self.assertFalse(r.is_reject)
        self.assertTrue(r.ok)

    def test_invalid_verdict_raises(self):
        with self.assertRaises(RuntimeError):
            self.client._to_result({"verdict": "MAYBE"})

    def test_flags_not_list(self):
        r = self.client._to_result({"verdict": "UNCERTAIN", "flags": "watermark"})
        self.assertEqual(r.verdict, VERDICT_UNCERTAIN)
        self.assertEqual(r.flags, ["watermark"])


class TestAutoReject(unittest.TestCase):
    def test_empty_allowlist_never_rejects(self):
        r = LocalVlmResult(verdict=VERDICT_REJECT, flags=["watermark"])
        self.assertTrue(r.is_reject)
        self.assertFalse(r.auto_reject_triggered(set()))

    def test_allowlist_match(self):
        r = LocalVlmResult(verdict=VERDICT_REJECT, flags=["watermark", "blur"])
        self.assertTrue(r.auto_reject_triggered({"watermark"}))

    def test_uncertain_never_rejects(self):
        r = LocalVlmResult(verdict=VERDICT_UNCERTAIN, flags=["watermark"])
        self.assertFalse(r.auto_reject_triggered({"watermark"}))

    def test_error_result_not_reject(self):
        r = LocalVlmResult(error="timeout")
        self.assertFalse(r.ok)
        self.assertFalse(r.is_reject)


class TestResize(unittest.TestCase):
    def test_resize_downscales(self):
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGB", (1280, 720), (10, 20, 30))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()
        out = _resize_jpeg(data, 512)
        out_img = Image.open(BytesIO(out))
        self.assertLessEqual(max(out_img.size), 512)

    def test_resize_disabled_returns_same(self):
        data = b"\xff\xd8fakejpeg"
        self.assertEqual(_resize_jpeg(data, 0), data)


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tracker = LocalVlmTracker(self.tmp)

    def test_false_reject_and_precision(self):
        loc_reject = LocalVlmResult(verdict=VERDICT_REJECT, flags=["watermark"], latency_sec=1.0)
        # MiMo признал подходящим -> false reject
        self.tracker.record(loc_reject, mimo_suitable=True, mimo_flags={}, mimo_category="",
                            mimo_confidence=0.9, mimo_error=False,
                            context={"video_id": "v1"})
        loc_reject2 = LocalVlmResult(verdict=VERDICT_REJECT, flags=["watermark"], latency_sec=2.0)
        # MiMo тоже отклонил -> matched
        self.tracker.record(loc_reject2, mimo_suitable=False, mimo_flags={"watermark": True},
                            mimo_category="", mimo_confidence=0.2, mimo_error=False,
                            context={"video_id": "v2"})
        s = self.tracker.summary()
        self.assertEqual(s["false_rejects"], 1)
        self.assertEqual(s["reject_matched"], 1)
        self.assertEqual(s["flags"]["watermark"]["precision"], 0.5)
        self.assertEqual(s["avg_latency_sec"], 1.5)

    def test_skipped_counts(self):
        loc = LocalVlmResult(verdict=VERDICT_REJECT, flags=["collage"])
        self.tracker.record(loc, mimo_suitable=None, mimo_flags={}, mimo_category="",
                            mimo_confidence=0.0, mimo_error=False, mimo_skipped=True,
                            context={"video_id": "v3"})
        s = self.tracker.summary()
        self.assertEqual(s["mimo_skipped"], 1)
        self.assertEqual(s["calls"], 1)

    def test_jsonl_written(self):
        loc = LocalVlmResult(verdict=VERDICT_NO_REJECT, flags=[], latency_sec=0.5)
        self.tracker.record(loc, mimo_suitable=True, mimo_flags={}, mimo_category="Физика",
                            mimo_confidence=0.8, mimo_error=False, context={"video_id": "v4"})
        self.assertTrue(self.tracker.jsonl.exists())
        lines = self.tracker.jsonl.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["video_id"], "v4")
        self.assertEqual(obj["mimo"]["suitable"], True)


class TestConfigDefaults(unittest.TestCase):
    def test_disabled_by_default(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.local_vlm_enabled)
        self.assertEqual(s.local_vlm_mode, "shadow")
        self.assertEqual(s.local_vlm_auto_reject_flags, [])


if __name__ == "__main__":
    unittest.main()
