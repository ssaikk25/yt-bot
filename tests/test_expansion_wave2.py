# -*- coding: utf-8 -*-
"""Тесты интеграции Expansion Wave 2 (900 запросов) в discovery."""
from __future__ import annotations

import unittest

from app.category_mapper import CATEGORY_KEY_MAP
from app.config import load_settings
from app.discovery import EXCLUDED_QUERY_CATEGORIES, DiscoveryEngine
from app.expansion_pool import EXPANSION_POOL, EXPERIMENT_POOL
from app.expansion_pool_950 import EXPANSION_950
from app.expansion_pool_wave2 import EXPANSION_WAVE2, load_expansion_wave2
from app.query_pool import QUERY_POOL, load_pool_from_yaml, normalize_query


class TestExpansionWave2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()
        cls.engine = DiscoveryEngine(cls.settings, None)

    # TEST 1
    def test_yaml_loads_900(self):
        self.assertEqual(len(EXPANSION_WAVE2), 900)

    # TEST 2
    def test_fields_complete(self):
        for q in EXPANSION_WAVE2:
            self.assertTrue(q["query"].strip())
            self.assertTrue(q["language"])
            self.assertTrue(q["category"])
            self.assertEqual(q["pool"], "expansion_wave2")
            self.assertIn("subtheme", q)
            self.assertIn("method", q)

    # TEST 3
    def test_no_internal_duplicates(self):
        norms = [normalize_query(q["query"]) for q in EXPANSION_WAVE2]
        self.assertEqual(len(norms), len(set(norms)))

    # TEST 4
    def test_no_duplicates_with_old_pool(self):
        old = [dict(q) for q in QUERY_POOL]
        old += [dict(q) for q in load_pool_from_yaml(self.settings)]
        old += [dict(q) for q in EXPANSION_POOL]
        old += [dict(q) for q in EXPERIMENT_POOL]
        old += [dict(q) for q in EXPANSION_950]
        old_norm = {normalize_query(str(q.get("query", ""))) for q in old}
        wave_norm = {normalize_query(q["query"]) for q in EXPANSION_WAVE2}
        self.assertFalse(old_norm & wave_norm)

    # TEST 5
    def test_old_pool_count_unchanged(self):
        old_raw = (len(QUERY_POOL) + len(load_pool_from_yaml(self.settings))
                   + len(EXPANSION_POOL) + len(EXPERIMENT_POOL) + len(EXPANSION_950))
        self.assertEqual(old_raw, 1472)

    # TEST 6
    def test_wave2_in_scheduler_pool(self):
        wave = [q for q in self.engine.pool.queries if q.get("pool") == "expansion_wave2"]
        self.assertEqual(len(wave), 850)  # 900 минус 50 аниме

    # TEST 7
    def test_scheduler_selects_wave2(self):
        wave = [q for q in self.engine.pool.queries if q.get("pool") == "expansion_wave2"]
        chosen = self.engine._select_pool(wave, 30)
        self.assertEqual(len(chosen), 30)
        self.assertTrue(all(q.get("pool") == "expansion_wave2" for q in chosen))

    # TEST 8
    def test_untried_detection(self):
        wave = [q for q in self.engine.pool.queries if q.get("pool") == "expansion_wave2"]
        # хотя бы часть новых запросов ещё не прогонялась
        untried = [q for q in wave if self.engine._is_untried(q)]
        self.assertGreater(len(untried), 0)

    # TEST 9
    def test_categories_are_canonical(self):
        cats = {q["category"] for q in EXPANSION_WAVE2}
        for c in cats:
            self.assertTrue(c in CATEGORY_KEY_MAP or c == "Аниме/Анимации", c)
        langs = {q["language"] for q in EXPANSION_WAVE2}
        self.assertEqual(len(langs), 40)

    # TEST 10
    def test_anime_excluded(self):
        anime = [q for q in EXPANSION_WAVE2 if q["category"] in EXCLUDED_QUERY_CATEGORIES]
        self.assertEqual(len(anime), 50)
        kept = [q for q in EXPANSION_WAVE2 if q["category"] not in EXCLUDED_QUERY_CATEGORIES]
        self.assertEqual(len(kept), 850)

    # TEST 11
    def test_pool_total(self):
        self.assertEqual(len(self.engine.pool.queries), 2241)

    # TEST 12
    def test_load_does_not_write_project_state(self):
        from pathlib import Path
        logs = Path(self.settings.logs_dir)
        targets = ["checked_channels.json", "discovery_seen_channels.json", "found_channels.json"]
        before = {}
        for name in targets:
            p = logs / name
            in_mtime = p.stat().st_mtime if p.exists() else None
            before[name] = in_mtime
        load_expansion_wave2()
        for name in targets:
            p = logs / name
            now_mtime = p.stat().st_mtime if p.exists() else None
            self.assertEqual(before[name], now_mtime, name)

    # TEST 13
    def test_load_idempotent(self):
        self.assertEqual(load_expansion_wave2(), load_expansion_wave2())


if __name__ == "__main__":
    unittest.main()
