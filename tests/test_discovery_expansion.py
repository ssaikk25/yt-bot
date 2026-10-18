# -*- coding: utf-8 -*-
"""Тесты расширения discovery: пулы, метаданные, dedupe, mix, статистика."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.discovery import DiscoveryEngine, QueryStat
from app.expansion_pool import EXPANSION_POOL, EXPERIMENT_POOL
from app.query_pool import QUERY_POOL, QueryPool, normalize_query


class TestPoolStructure(unittest.TestCase):
    def test_base_pool_unchanged(self):
        # BASE_POOL не тронут (127 встроенных запросов)
        self.assertEqual(len(QUERY_POOL), 127)

    def test_expansion_pool_exists(self):
        self.assertGreater(len(EXPANSION_POOL), 200)

    def test_experiment_pool_exists(self):
        self.assertGreaterEqual(len(EXPERIMENT_POOL), 50)

    def test_expansion_metadata_present(self):
        for q in EXPANSION_POOL:
            self.assertTrue(q.get("query"), f"нет query у {q}")
            self.assertTrue(q.get("language"), f"нет language у {q}")
            self.assertTrue(q.get("category"), f"нет theme у {q}")
            self.assertTrue(q.get("subtheme"), f"нет subtheme у {q}")
            self.assertEqual(q.get("pool"), "expansion")

    def test_experiment_metadata_present(self):
        for q in EXPERIMENT_POOL:
            self.assertEqual(q.get("pool"), "experiment")
            self.assertTrue(q.get("query"))
            self.assertTrue(q.get("category"))

    def test_languages_covered(self):
        langs = {q["language"] for q in EXPANSION_POOL}
        for need in ("en", "es", "pt", "de", "fr", "it", "pl", "tr", "cs", "ro", "uk", "ja", "ko", "id", "vi"):
            self.assertIn(need, langs, f"нет языка {need}")


class TestDedupe(unittest.TestCase):
    def test_exact_normalized_dedupe(self):
        pool = QueryPool([
            {"query": "lathe turning metal", "language": "en"},
            {"query": "  Lathe   Turning  Metal ", "language": "en"},  # дубль после normalize
        ])
        self.assertEqual(pool.size(), 1)

    def test_different_languages_kept(self):
        pool = QueryPool([
            {"query": "lathe turning metal", "language": "en"},
            {"query": "torno metal pieza", "language": "es"},  # семантически похоже, но другой язык
        ])
        self.assertEqual(pool.size(), 2)

    def test_normalize(self):
        self.assertEqual(normalize_query("  Lathe   Turning "), "lathe turning")


class TestQueryMix(unittest.TestCase):
    def _engine(self):
        tmp = tempfile.mkdtemp()
        settings = Settings(
            logs_dir=tmp, temp_dir=tmp,
            discovery_queries_per_run=100,
            base_query_ratio=0.70, expansion_query_ratio=0.20, experiment_query_ratio=0.10,
        )
        return DiscoveryEngine(settings, None)

    def test_mix_respects_ratio(self):
        eng = self._engine()
        mixed = eng._mixed_queries()
        n_base = sum(1 for q in mixed if q.get("pool") == "base")
        n_exp = sum(1 for q in mixed if q.get("pool") == "expansion")
        n_exr = sum(1 for q in mixed if q.get("pool") == "experiment")
        self.assertEqual(n_base, 70)
        self.assertEqual(n_exp, 20)
        self.assertEqual(n_exr, 10)
        self.assertEqual(len(mixed), 100)

    def test_select_pool_bounded(self):
        eng = self._engine()
        qs = [{"_norm": f"q{i}", "weight": 0.5} for i in range(30)]
        chosen = eng._select_pool(qs, 10)
        self.assertEqual(len(chosen), 10)

    def test_select_pool_theme_diversity(self):
        # при n >= кол-ву тем — каждая тема должна быть представлена (Phase 1 explore)
        eng = self._engine()
        qs = []
        for t in range(5):
            for i in range(10):
                qs.append({"_norm": f"{t}-{i}", "weight": 0.5, "category": f"theme{t}"})
        chosen = eng._select_pool(qs, 5)
        themes = {q["category"] for q in chosen}
        self.assertEqual(len(themes), 5)

    def test_select_pool_no_dupes(self):
        eng = self._engine()
        qs = [{"_norm": f"q{i}", "weight": 0.5, "category": "t"} for i in range(20)]
        chosen = eng._select_pool(qs, 15)
        norms = [q["_norm"] for q in chosen]
        self.assertEqual(len(norms), len(set(norms)))


class TestQueryStats(unittest.TestCase):
    def test_to_dict_has_new_fields(self):
        st = QueryStat(norm="n", query="q", language="en", pool="expansion",
                       theme="metalworking", subtheme="lathe_turning",
                       new_channels=3, unique_channels=10, runs=2, new_ratio=0.3,
                       priority_ema=0.4)
        d = st.to_dict()
        for k in ("pool", "theme", "subtheme", "runs", "new_ratio", "priority_ema"):
            self.assertIn(k, d)

    def test_aggregate_stats_files(self):
        tmp = tempfile.mkdtemp()
        settings = Settings(logs_dir=tmp, temp_dir=tmp)
        eng = DiscoveryEngine(settings, None)
        eng.stats = {
            "a": QueryStat(norm="a", language="en", theme="metalworking", subtheme="lathe", new_channels=5, unique_channels=10),
            "b": QueryStat(norm="b", language="de", theme="metalworking", subtheme="lathe", new_channels=2, unique_channels=8),
            "c": QueryStat(norm="c", language="en", theme="garden", subtheme="greenhouse", new_channels=0, unique_channels=4),
        }
        eng._save_aggregate_stats()
        lang = json.loads(Path(tmp, "language_stats.json").read_text(encoding="utf-8"))
        theme = json.loads(Path(tmp, "theme_stats.json").read_text(encoding="utf-8"))
        self.assertIn("en", lang)
        self.assertIn("de", lang)
        self.assertIn("metalworking", theme)
        self.assertIn("garden", theme)
        # en: 5+0 new / 10+4 unique
        self.assertEqual(lang["en"]["new_channels"], 5)
        self.assertEqual(lang["en"]["unique_channels"], 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
