"""QUERY POOL — база long-tail запросов на локальных языках + выбор запросов.

Принципы:
- long-tail (объект + действие + локальность), а не широкие темы;
- приоритет локальных языков (PL/CS/SK/RO/BG/HU/SR/HR), затем DE/FR/ES/IT/PT/NL/EL/TR/UK/RU/EN;
- никакого префикса "youtube.com" — ytsearch уже ищет на YouTube;
- нормализация + дедупликация запросов;
- выбор по схеме exploit/explore/random.
"""
from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

# Каждый запрос: query, language (ISO), region (regionCode), category (тег).
# weight — начальный приоритет (локальные языки выше).
QUERY_POOL: list[dict[str, Any]] = [
    # --- Polish (PL) ---
    {"query": "toczenie metalu mały element", "language": "pl", "region": "PL", "category": "metalworking", "weight": 1.0},
    {"query": "frezowanie mały detal warsztat", "language": "pl", "region": "PL", "category": "machining", "weight": 1.0},
    {"query": "kowalstwo kucie żelaza", "language": "pl", "region": "PL", "category": "forging", "weight": 1.0},
    {"query": "traktor orka pola wieś", "language": "pl", "region": "PL", "category": "agriculture", "weight": 1.0},
    {"query": "praca koparki kamieniołom", "language": "pl", "region": "PL", "category": "excavation", "weight": 1.0},
    {"query": "pasieka przegląd uli", "language": "pl", "region": "PL", "category": "beekeeping", "weight": 1.0},
    {"query": "koło garncarskie wyrób naczynia", "language": "pl", "region": "PL", "category": "pottery", "weight": 1.0},
    {"query": "obróbka drewna mały warsztat", "language": "pl", "region": "PL", "category": "woodworking", "weight": 1.0},
    {"query": "odlewanie metalu mała odlewnia", "language": "pl", "region": "PL", "category": "foundry", "weight": 1.0},
    {"query": "naprawa starej maszyny rolniczej", "language": "pl", "region": "PL", "category": "machinery", "weight": 1.0},
    # --- Czech (CS) ---
    {"query": "soustružení kovu malý díl", "language": "cs", "region": "CZ", "category": "metalworking", "weight": 1.0},
    {"query": "frézování malý detail dílna", "language": "cs", "region": "CZ", "category": "machining", "weight": 1.0},
    {"query": "kovářství kování železa", "language": "cs", "region": "CZ", "category": "forging", "weight": 1.0},
    {"query": "traktor orání pole vesnice", "language": "cs", "region": "CZ", "category": "agriculture", "weight": 1.0},
    {"query": "práce bagru lom", "language": "cs", "region": "CZ", "category": "excavation", "weight": 1.0},
    {"query": "včelařství kontrola úlů", "language": "cs", "region": "CZ", "category": "beekeeping", "weight": 1.0},
    {"query": "hrnčířský kruh výroba nádoby", "language": "cs", "region": "CZ", "category": "pottery", "weight": 1.0},
    {"query": "oprava starého zemědělského stroje", "language": "cs", "region": "CZ", "category": "machinery", "weight": 1.0},
    {"query": "obrábění dřeva dílna", "language": "cs", "region": "CZ", "category": "woodworking", "weight": 1.0},
    # --- Slovak (SK) ---
    {"query": "sústruženie kovu malý diel", "language": "sk", "region": "SK", "category": "metalworking", "weight": 1.0},
    {"query": "frézovanie detailu dielňa", "language": "sk", "region": "SK", "category": "machining", "weight": 1.0},
    {"query": "kováčstvo kovanie železa", "language": "sk", "region": "SK", "category": "forging", "weight": 1.0},
    {"query": "traktor orba poľa dedina", "language": "sk", "region": "SK", "category": "agriculture", "weight": 1.0},
    {"query": "práca bagra lom", "language": "sk", "region": "SK", "category": "excavation", "weight": 1.0},
    {"query": "včelárstvo kontrola úľov", "language": "sk", "region": "SK", "category": "beekeeping", "weight": 1.0},
    {"query": "hrnčiarsky kruh výroba nádoby", "language": "sk", "region": "SK", "category": "pottery", "weight": 1.0},
    {"query": "oprava starého poľnohospodárskeho stroja", "language": "sk", "region": "SK", "category": "machinery", "weight": 1.0},
    {"query": "obrábanie dreva dielňa", "language": "sk", "region": "SK", "category": "woodworking", "weight": 1.0},
    # --- Romanian (RO) ---
    {"query": "strunjire metal piesă mică", "language": "ro", "region": "RO", "category": "metalworking", "weight": 1.0},
    {"query": "frezare piesă atelier mic", "language": "ro", "region": "RO", "category": "machining", "weight": 1.0},
    {"query": "forjare fier atelier", "language": "ro", "region": "RO", "category": "forging", "weight": 1.0},
    {"query": "tractor ară câmp sat", "language": "ro", "region": "RO", "category": "agriculture", "weight": 1.0},
    {"query": "excavator lucrează carieră", "language": "ro", "region": "RO", "category": "excavation", "weight": 1.0},
    {"query": "stupină verificare stupi", "language": "ro", "region": "RO", "category": "beekeeping", "weight": 1.0},
    {"query": "roată olar vas", "language": "ro", "region": "RO", "category": "pottery", "weight": 1.0},
    {"query": "reparare utilaj agricol vechi", "language": "ro", "region": "RO", "category": "machinery", "weight": 1.0},
    {"query": "prelucrare lemn atelier", "language": "ro", "region": "RO", "category": "woodworking", "weight": 1.0},
    {"query": "turnare metal mică turnătorie", "language": "ro", "region": "RO", "category": "foundry", "weight": 1.0},
    # --- Bulgarian (BG) ---
    {"query": "струговане метал малък детайл", "language": "bg", "region": "BG", "category": "metalworking", "weight": 1.0},
    {"query": "фрезоване детайл работилница", "language": "bg", "region": "BG", "category": "machining", "weight": 1.0},
    {"query": "ковачество коване желязо", "language": "bg", "region": "BG", "category": "forging", "weight": 1.0},
    {"query": "трактор оран поле село", "language": "bg", "region": "BG", "category": "agriculture", "weight": 1.0},
    {"query": "багер работи кариера", "language": "bg", "region": "BG", "category": "excavation", "weight": 1.0},
    {"query": "пчеларство преглед на кошери", "language": "bg", "region": "BG", "category": "beekeeping", "weight": 1.0},
    {"query": "грънчарско колело изработка съд", "language": "bg", "region": "BG", "category": "pottery", "weight": 1.0},
    {"query": "ремонт стар трактор", "language": "bg", "region": "BG", "category": "machinery", "weight": 1.0},
    {"query": "обработка на дърво работилница", "language": "bg", "region": "BG", "category": "woodworking", "weight": 1.0},
    {"query": "леене на метал малка леярна", "language": "bg", "region": "BG", "category": "foundry", "weight": 1.0},
    # --- Hungarian (HU) ---
    {"query": "esztergálás fém kis alkatrész", "language": "hu", "region": "HU", "category": "metalworking", "weight": 1.0},
    {"query": "marás kis alkatrész műhely", "language": "hu", "region": "HU", "category": "machining", "weight": 1.0},
    {"query": "kovácsolás vas műhely", "language": "hu", "region": "HU", "category": "forging", "weight": 1.0},
    {"query": "traktor szántás falu", "language": "hu", "region": "HU", "category": "agriculture", "weight": 1.0},
    {"query": "kotrógép kőbánya munka", "language": "hu", "region": "HU", "category": "excavation", "weight": 1.0},
    {"query": "méhészet kaptár ellenőrzés", "language": "hu", "region": "HU", "category": "beekeeping", "weight": 1.0},
    {"query": "fazekas korong edény készítés", "language": "hu", "region": "HU", "category": "pottery", "weight": 1.0},
    {"query": "régi mezőgazdasági gép javítás", "language": "hu", "region": "HU", "category": "machinery", "weight": 1.0},
    {"query": "fa megmunkálás műhely", "language": "hu", "region": "HU", "category": "woodworking", "weight": 1.0},
    {"query": "fémöntés kis öntöde", "language": "hu", "region": "HU", "category": "foundry", "weight": 1.0},
    # --- German (DE) ---
    {"query": "Metall drehen kleines Teil Werkstatt", "language": "de", "region": "DE", "category": "metalworking", "weight": 0.9},
    {"query": "Fräsen kleines Bauteil Werkstatt", "language": "de", "region": "DE", "category": "machining", "weight": 0.9},
    {"query": "Schmieden Eisen kleine Werkstatt", "language": "de", "region": "DE", "category": "forging", "weight": 0.9},
    {"query": "Traktor pflügt Feld Dorf", "language": "de", "region": "DE", "category": "agriculture", "weight": 0.9},
    {"query": "Bagger arbeitet Steinbruch", "language": "de", "region": "DE", "category": "excavation", "weight": 0.9},
    {"query": "Imkerei Wabenkontrolle", "language": "de", "region": "DE", "category": "beekeeping", "weight": 0.9},
    {"query": "Töpferscheibe Gefäß herstellen", "language": "de", "region": "DE", "category": "pottery", "weight": 0.9},
    {"query": "alte Landmaschine reparieren", "language": "de", "region": "DE", "category": "machinery", "weight": 0.9},
    {"query": "Holzbearbeitung kleine Werkstatt", "language": "de", "region": "DE", "category": "woodworking", "weight": 0.9},
    {"query": "Metallguss kleine Gießerei", "language": "de", "region": "DE", "category": "foundry", "weight": 0.9},
    # --- French (FR) ---
    {"query": "tournage métal petite pièce atelier", "language": "fr", "region": "FR", "category": "metalworking", "weight": 0.9},
    {"query": "fraisage petite pièce atelier", "language": "fr", "region": "FR", "category": "machining", "weight": 0.9},
    {"query": "forge fer petit atelier", "language": "fr", "region": "FR", "category": "forging", "weight": 0.9},
    {"query": "tracteur labour champ village", "language": "fr", "region": "FR", "category": "agriculture", "weight": 0.9},
    {"query": "pelle travaille carrière", "language": "fr", "region": "FR", "category": "excavation", "weight": 0.9},
    {"query": "apiculture contrôle des ruches", "language": "fr", "region": "FR", "category": "beekeeping", "weight": 0.9},
    {"query": "tour de potier fabriquer vase", "language": "fr", "region": "FR", "category": "pottery", "weight": 0.9},
    {"query": "réparer vieux engin agricole", "language": "fr", "region": "FR", "category": "machinery", "weight": 0.9},
    {"query": "travail du bois petit atelier", "language": "fr", "region": "FR", "category": "woodworking", "weight": 0.9},
    {"query": "fonderie métal petit atelier", "language": "fr", "region": "FR", "category": "foundry", "weight": 0.9},
    # --- Spanish (ES) ---
    {"query": "torneado metal pieza pequeña taller", "language": "es", "region": "ES", "category": "metalworking", "weight": 0.85},
    {"query": "fresado pieza pequeña taller", "language": "es", "region": "ES", "category": "machining", "weight": 0.85},
    {"query": "forja hierro pequeño taller", "language": "es", "region": "ES", "category": "forging", "weight": 0.85},
    {"query": "tractor arando campo pueblo", "language": "es", "region": "ES", "category": "agriculture", "weight": 0.85},
    {"query": "excavadora trabajando cantera", "language": "es", "region": "ES", "category": "excavation", "weight": 0.85},
    {"query": "apicultura revisión de colmenas", "language": "es", "region": "ES", "category": "beekeeping", "weight": 0.85},
    {"query": "torno alfarero hacer vasija", "language": "es", "region": "ES", "category": "pottery", "weight": 0.85},
    {"query": "reparar maquinaria agrícola antigua", "language": "es", "region": "ES", "category": "machinery", "weight": 0.85},
    {"query": "trabajo de madera pequeño taller", "language": "es", "region": "ES", "category": "woodworking", "weight": 0.85},
    {"query": "fundición metal pequeña fundición", "language": "es", "region": "ES", "category": "foundry", "weight": 0.85},
    # --- Italian (IT) ---
    {"query": "tornitura metallo piccolo pezzo officina", "language": "it", "region": "IT", "category": "metalworking", "weight": 0.85},
    {"query": "fresatura piccolo pezzo officina", "language": "it", "region": "IT", "category": "machining", "weight": 0.85},
    {"query": "forgiatura ferro piccola officina", "language": "it", "region": "IT", "category": "forging", "weight": 0.85},
    {"query": "trattore aratura campo paese", "language": "it", "region": "IT", "category": "agriculture", "weight": 0.85},
    {"query": "escavatore lavora cava", "language": "it", "region": "IT", "category": "excavation", "weight": 0.85},
    {"query": "apicoltura controllo alveari", "language": "it", "region": "IT", "category": "beekeeping", "weight": 0.85},
    {"query": "tornio da vasaio vaso", "language": "it", "region": "IT", "category": "pottery", "weight": 0.85},
    {"query": "riparazione vecchia macchina agricola", "language": "it", "region": "IT", "category": "machinery", "weight": 0.85},
    {"query": "lavorazione legno piccola officina", "language": "it", "region": "IT", "category": "woodworking", "weight": 0.85},
    {"query": "fusione metallo piccola fonderia", "language": "it", "region": "IT", "category": "foundry", "weight": 0.85},
    # --- Russian (RU) ---
    {"query": "токарный станок обработка металла", "language": "ru", "region": "RU", "category": "metalworking", "weight": 0.8},
    {"query": "фрезерный станок ЧПУ обработка детали", "language": "ru", "region": "RU", "category": "machining", "weight": 0.8},
    {"query": "ковка металла вручную кузница", "language": "ru", "region": "RU", "category": "forging", "weight": 0.8},
    {"query": "трактор вспашка поля деревня", "language": "ru", "region": "RU", "category": "agriculture", "weight": 0.8},
    {"query": "экскаватор копает котлован работа", "language": "ru", "region": "RU", "category": "excavation", "weight": 0.8},
    {"query": "осмотр пчелиных рамок пасека", "language": "ru", "region": "RU", "category": "beekeeping", "weight": 0.8},
    {"query": "гончарный круг лепка кувшина", "language": "ru", "region": "RU", "category": "pottery", "weight": 0.8},
    {"query": "резьба по дереву мастерская", "language": "ru", "region": "RU", "category": "woodworking", "weight": 0.8},
    {"query": "пересадка рассады томатов теплица", "language": "ru", "region": "RU", "category": "gardening", "weight": 0.8},
    {"query": "выпечка хлеба в дровяной печи", "language": "ru", "region": "RU", "category": "cooking", "weight": 0.8},
    {"query": "поезд из кабины машиниста", "language": "ru", "region": "RU", "category": "train", "weight": 0.8},
    {"query": "arduino самодельный станок ЧПУ", "language": "ru", "region": "RU", "category": "electronics", "weight": 0.8},
    # --- English (EN) ---
    {"query": "small lathe turning steel part", "language": "en", "region": None, "category": "metalworking", "weight": 0.6},
    {"query": "garage lathe machining bushing", "language": "en", "region": None, "category": "machining", "weight": 0.6},
    {"query": "blacksmith forging hammer village", "language": "en", "region": None, "category": "forging", "weight": 0.6},
    {"query": "old tractor plowing field", "language": "en", "region": None, "category": "agriculture", "weight": 0.6},
    {"query": "small apiary hive inspection", "language": "en", "region": None, "category": "beekeeping", "weight": 0.6},
    {"query": "pottery wheel making vase workshop", "language": "en", "region": None, "category": "pottery", "weight": 0.6},
    {"query": "wood carving small workshop", "language": "en", "region": None, "category": "woodworking", "weight": 0.6},
    {"query": "small hydraulic press workshop", "language": "en", "region": None, "category": "machinery", "weight": 0.6},
    {"query": "old agricultural machine repair", "language": "en", "region": None, "category": "machinery", "weight": 0.6},
    {"query": "home foundry casting metal", "language": "en", "region": None, "category": "foundry", "weight": 0.6},
    {"query": "train cab view countryside", "language": "en", "region": None, "category": "train", "weight": 0.6},
    {"query": "drone flight coastline 4k", "language": "en", "region": None, "category": "drone", "weight": 0.6},
    {"query": "walking tour small town", "language": "en", "region": None, "category": "gopro", "weight": 0.6},
    {"query": "baking bread wood fired oven", "language": "en", "region": None, "category": "cooking", "weight": 0.6},
    {"query": "street food cooking wok", "language": "en", "region": None, "category": "food", "weight": 0.6},
    {"query": "arduino homemade CNC machine", "language": "en", "region": None, "category": "electronics", "weight": 0.6},
    {"query": "3d printer timelapse print", "language": "en", "region": None, "category": "3dprinting", "weight": 0.6},
]


def normalize_query(q: str) -> str:
    """Нормализация запроса: lower, unicode NFKC, схлопывание пробелов."""
    q = unicodedata.normalize("NFKC", (q or ""))
    q = q.lower()
    q = re.sub(r"\s+", " ", q).strip()
    return q


@dataclass
class QueryPool:
    """Пул запросов с выбором по схеме exploit/explore/random и учётом скоринга."""
    queries: list[dict[str, Any]] = field(default_factory=lambda: [dict(q) for q in QUERY_POOL])
    scores: dict[str, float] = field(default_factory=dict)  # normalized_query -> score
    visited: set[str] = field(default_factory=set)

    def __post_init__(self):
        # нормализуем и убираем дубликаты
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for q in self.queries:
            q = dict(q)
            q["_norm"] = normalize_query(q.get("query", ""))
            if not q["_norm"] or q["_norm"] in seen:
                continue
            seen.add(q["_norm"])
            uniq.append(q)
        self.queries = uniq

    def size(self) -> int:
        return len(self.queries)

    def record(self, norm: str, score: float) -> None:
        self.scores[norm] = score
        self.visited.add(norm)

    def select(self, settings, count: int, cycle: int) -> list[dict[str, Any]]:
        """Выбор запросов: exploit (best) + explore (новые) + random."""
        if not self.queries:
            return []
        best = sorted(
            self.queries,
            key=lambda q: (self.scores.get(q["_norm"], q.get("weight", 0.5)), q["_norm"]),
            reverse=True,
        )
        n_exploit = max(0, int(round(count * settings.exploit_ratio)))
        n_explore = max(0, int(round(count * settings.explore_ratio)))
        n_random = max(0, count - n_exploit - n_explore)

        chosen: list[dict[str, Any]] = []
        chosen_norms: set[str] = set()

        for q in best:
            if len(chosen) >= n_exploit:
                break
            if q["_norm"] not in chosen_norms:
                chosen.append(q)
                chosen_norms.add(q["_norm"])

        unvisited = [q for q in self.queries if q["_norm"] not in self.visited and q["_norm"] not in chosen_norms]
        random.shuffle(unvisited)
        for q in unvisited:
            if len(chosen) >= n_exploit + n_explore:
                break
            if q["_norm"] not in chosen_norms:
                chosen.append(q)
                chosen_norms.add(q["_norm"])

        rest = [q for q in self.queries if q["_norm"] not in chosen_norms]
        random.shuffle(rest)
        for q in rest:
            if len(chosen) >= count:
                break
            if q["_norm"] not in chosen_norms:
                chosen.append(q)
                chosen_norms.add(q["_norm"])

        return chosen[:count]


def load_pool_from_yaml(settings) -> list[dict[str, Any]]:
    """Совместимость: запросы из YAML тоже участвуют (с тегом 'yaml')."""
    out: list[dict[str, Any]] = []
    for q in settings.queries:
        d = {
            "query": q.get("query", ""),
            "language": q.get("language"),
            "region": q.get("region"),
            "category": "yaml",
            "weight": 0.5,
            "video_duration": q.get("video_duration"),
        }
        out.append(d)
    return out
