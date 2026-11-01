# -*- coding: utf-8 -*-
"""Тесты интеграции Expansion Wave 3 (900 запросов) в discovery."""
from __future__ import annotations

import unittest

from app.category_mapper import CATEGORY_KEY_MAP
from app.config import load_settings
from app.discovery import EXCLUDED_QUERY_CATEGORIES, DiscoveryEngine
from app.expansion_pool import EXPANSION_POOL, EXPERIMENT_POOL
from app.expansion_pool_950 import EXPANSION_950
from app.expansion_pool_wave2 import EXPANSION_WAVE2
from app.expansion_pool_wave3 import EXPANSION_WAVE3, load_expansion_wave3
from app.query_pool import QUERY_POOL, load_pool_from_yaml, normalize_query


class TestExpansionWave3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()
        cls.engine = DiscoveryEngine(cls.settings, None)

    def test_yaml_loads_900(self):
        self.assertEqual(len(EXPANSION_WAVE3), 900)

    def test_18_categories_x_50(self):
        from collections import Counter
        cats = Counter(q["category"] for q in EXPANSION_WAVE3)
        self.assertEqual(len(cats), 18)
        self.assertEqual(set(cats.values()), {50})

    def test_10_languages_x_90(self):
        from collections import Counter
        langs = Counter(q["language"] for q in EXPANSION_WAVE3)
        self.assertEqual(len(langs), 10)
        self.assertEqual(set(langs.values()), {90})
        for need in ("th", "vi", "hi", "ar", "fa", "el", "he", "ms", "tl", "bn"):
            self.assertIn(need, langs)

    def test_no_internal_duplicates(self):
        norms = [normalize_query(q["query"]) for q in EXPANSION_WAVE3]
        self.assertEqual(len(norms), len(set(norms)))

    def test_no_duplicates_with_existing(self):
        old = [dict(q) for q in QUERY_POOL]
        old += [dict(q) for q in load_pool_from_yaml(self.settings)]
        old += [dict(q) for q in EXPANSION_POOL]
        old += [dict(q) for q in EXPERIMENT_POOL]
        old += [dict(q) for q in EXPANSION_950]
        old += [dict(q) for q in EXPANSION_WAVE2]
        old_norm = {normalize_query(str(q.get("query", ""))) for q in old}
        wave_norm = {normalize_query(q["query"]) for q in EXPANSION_WAVE3}
        self.assertFalse(old_norm & wave_norm)

    def test_categories_canonical(self):
        cats = {q["category"] for q in EXPANSION_WAVE3}
        for c in cats:
            self.assertTrue(c in CATEGORY_KEY_MAP or c == "Аниме/Анимации", c)

    def test_anime_excluded(self):
        anime = [q for q in EXPANSION_WAVE3 if q["category"] in EXCLUDED_QUERY_CATEGORIES]
        self.assertEqual(len(anime), 50)
        kept = [q for q in EXPANSION_WAVE3 if q["category"] not in EXCLUDED_QUERY_CATEGORIES]
        self.assertEqual(len(kept), 850)

    def test_wave3_in_scheduler_pool(self):
        wave = [q for q in self.engine.pool.queries if q.get("pool") == "expansion_wave3"]
        self.assertEqual(len(wave), 850)

    def test_scheduler_selects_wave3(self):
        wave = [q for q in self.engine.pool.queries if q.get("pool") == "expansion_wave3"]
        chosen = self.engine._select_pool(wave, 30)
        self.assertEqual(len(chosen), 30)
        self.assertTrue(all(q.get("pool") == "expansion_wave3" for q in chosen))

    def test_wave3_in_mixed_queries(self):
        mixed = self.engine._mixed_queries()
        wave3 = [q for q in mixed if q.get("pool") == "expansion_wave3"]
        self.assertGreater(len(wave3), 0)

    def test_pool_total(self):
        self.assertEqual(len(self.engine.pool.queries), 3091)

    def test_load_does_not_write_project_state(self):
        from pathlib import Path
        logs = Path(self.settings.logs_dir)
        targets = ["checked_channels.json", "discovery_seen_channels.json", "found_channels.json"]
        before = {}
        for name in targets:
            p = logs / name
            before[name] = p.stat().st_mtime if p.exists() else None
        load_expansion_wave3()
        for name in targets:
            p = logs / name
            self.assertEqual(before[name], p.stat().st_mtime if p.exists() else None, name)

    def test_load_idempotent(self):
        self.assertEqual(load_expansion_wave3(), load_expansion_wave3())

    def test_old_pool_unchanged(self):
        old_raw = (len(QUERY_POOL) + len(load_pool_from_yaml(self.settings))
                   + len(EXPANSION_POOL) + len(EXPERIMENT_POOL) + len(EXPANSION_950))
        self.assertEqual(old_raw, 1472)
        self.assertEqual(len(EXPANSION_WAVE2), 900)
        self.assertEqual(len(EXPANSION_WAVE3), 900)


if __name__ == "__main__":
    unittest.main()
