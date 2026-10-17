"""The original route_maker CSV interpretation pipeline, with explicit GIS gaps.

Factual calendars/planet positions come from the existing precise providers.
Each source is interpreted independently, saturated and rounded as in the
original. Their event/quality scores are then averaged BEFORE the full 6 x 8
street matrix. The current real-street demo can route on four measured proxies;
the other four source weights remain visible but are omitted from route cost.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path


VERSION = "route-maker-reading-v1"
FUSION_POLICY = "route-maker-quality-mean-v1"
CONFIG = Path(__file__).with_name("route_maker_rules")
EVENTS = ("encounter", "opportunity", "discovery")
QUALITIES = ("activation", "receptivity", "clarity", "expansion", "grounding", "flow")
STREETS = ("green", "water", "sunlight", "open_sky", "quiet", "vibrant", "rest", "culture")
ROUTING_AXES = ("green", "quiet", "open_sky", "vibrant")
PLANETS = ("sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
ASPECTS = {"conjunction": (0, 6., 1.), "sextile": (60, 4., .6),
           "square": (90, 5., .7), "trine": (120, 5., .9), "opposition": (180, 6., .8)}
NATAL_PLANET_FACTOR = .5
MIN_ASPECT_STRENGTH = .35
MAX_ASPECTS = 6
_STEM_EN = dict(zip("甲乙丙丁戊己庚辛壬癸", ("Jia", "Yi", "Bing", "Ding", "Wu", "Ji", "Geng", "Xin", "Ren", "Gui")))
_BRANCH_EN = dict(zip("子丑寅卯辰巳午未申酉戌亥", ("Zi", "Chou", "Yin", "Mao", "Chen", "Si", "Wu", "Wei", "Shen", "You", "Xu", "Hai")))
_TEN_GOD_EN = {"비견": "Friend", "겁재": "Rob Wealth", "식신": "Eating God", "상관": "Hurting Officer",
               "정재": "Direct Wealth", "편재": "Indirect Wealth", "정관": "Direct Officer",
               "편관": "Seven Killings", "정인": "Direct Resource", "편인": "Indirect Resource"}
_COMBINATIONS = {frozenset(pair) for pair in ("子丑", "寅亥", "卯戌", "辰酉", "巳申", "午未")}
_CLASHES = {frozenset(pair) for pair in ("子午", "丑未", "寅申", "卯酉", "辰戌", "巳亥")}
_ELEMENTS = ("wood", "fire", "earth", "metal", "water")


@lru_cache(maxsize=3)
def load_rules(path):
    index = {}
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for raw in csv.DictReader(stream):
            row = dict(raw)
            row["weight"] = float(row.get("weight", row.get("base_weight")))
            index.setdefault((row["signal_type"], row["signal"]), []).append(row)
    return index


class Accumulator:
    def __init__(self, source):
        self.source = source
        self.event = dict.fromkeys(EVENTS, 0.)
        self.quality = dict.fromkeys(QUALITIES, 0.)
        self.evidence = []

    def fire(self, rules, signal_type, signal, strength, detail):
        for rule in rules.get((signal_type, signal), []):
            score = rule["weight"] * strength
            self.event[rule["event"]] += score
            self.quality[rule["quality"]] += score
            self.evidence.append({
                "source": self.source, "signal_type": signal_type, "signal": signal,
                "detail": detail, "event": rule["event"], "quality": rule["quality"],
                "score": round(score, 3), "strength": strength, "rule_weight": rule["weight"],
                "explanation": rule["explanation"],
                "basis_type": rule.get("basis_type", "no_external_basis"),
                "reference": rule.get("reference", ""),
                "verification_status": rule.get("verification_status", "pending"),
                "verification_scope": "Symbolic background only; numeric weights and route benefits are not validated.",
            })

    def result(self):
        return {name: {key: round(1 - math.exp(-value), 3) for key, value in values.items()}
                for name, values in (("event", self.event), ("quality", self.quality))}


def interpret_saju(raw, rules):
    acc = Accumulator("saju")
    sig = raw["signals"]
    today_stem = raw["today"]["pillar"]["stem"]
    day_master = raw["natal"]["day_master"]["stem"]
    acc.fire(rules, "ten_god", sig["daily_ten_god"], 1.,
             f"Today's stem ({_STEM_EN.get(today_stem, today_stem)}) is "
             f"{_TEN_GOD_EN.get(sig['daily_ten_god'], sig['daily_ten_god'])} relative to "
             f"the day master ({_STEM_EN.get(day_master, day_master)}).")
    for element in sig["active_elements"]:
        acc.fire(rules, "element", element, 1., f"{element.title()} is active in today's pillar.")
    for item in sig["interactions"]:
        verb = "combination (He)" if item["type"] == "combination" else "clash (Chong)"
        acc.fire(rules, "interaction", item["type"], 1.,
                 f"Today's branch ({_BRANCH_EN.get(item['today_branch'], item['today_branch'])}) forms a {verb} "
                 f"with the natal {item['natal_pillar']} branch ({_BRANCH_EN.get(item['natal_branch'], item['natal_branch'])}).")
    for element in sig["deficient_elements_active"]:
        acc.fire(rules, "natal_balance", "deficient_element_active", 1.,
                 f"{element.title()}, absent from the confirmed natal pillars, arrives today.")
    for element in sig["excess_elements_active"]:
        acc.fire(rules, "natal_balance", "excess_element_active", 1.,
                 f"{element.title()}, counted at least three times in the confirmed natal pillars, is active today.")
    return acc


def interpret_astrology(raw, rules):
    acc = Accumulator("astrology")
    aspects = [item for item in raw["aspects"] if item["strength"] >= MIN_ASPECT_STRENGTH]
    for item in aspects[:MAX_ASPECTS]:
        detail = (f"Transit {item['transit'].title()} {item['aspect']} natal {item['natal'].title()} "
                  f"(orb {item['orb']}°, strength {item['strength']}).")
        acc.fire(rules, "planet", item["transit"], item["strength"], detail)
        acc.fire(rules, "planet", item["natal"], item["strength"] * NATAL_PLANET_FACTOR, detail)
    for element in raw["signals"]["active_elements"]:
        acc.fire(rules, "element", element, 1., f"Transit Sun/Moon sign element: {element}.")
    return acc


def combine_scores(a, b):
    """Match the original saturation-first, round-three-decimals mean exactly."""
    return {section: {key: round((a[section][key] + b[section][key]) / 2, 3)
                      for key in keys} for section, keys in (("event", EVENTS), ("quality", QUALITIES))}


def street_weights(quality):
    out = dict.fromkeys(STREETS, 0.)
    with (CONFIG / "quality_street_matrix.csv").open(newline="", encoding="utf-8") as stream:
        rows = csv.reader(stream)
        columns = next(rows)[1:]
        if tuple(columns) != STREETS:
            raise ValueError("Street matrix columns do not match the original contract.")
        for row in rows:
            for column, value in zip(columns, map(float, row[1:])):
                out[column] += quality.get(row[0], 0.) * value
    total = sum(out.values()) or 1.
    return {key: round(value / total, 4) for key, value in out.items()}


def routing_projection(source_weights):
    total = sum(source_weights[key] for key in ROUTING_AXES)
    if not total or not math.isfinite(total):
        raise ValueError("No supported street weights are available.")
    # No further quantization: the route request must retain these exact values.
    return {key: source_weights[key] / total for key in ROUTING_AXES}


def find_aspects(transit, natal):
    """Original nine-planet aspect strength/orb rules and stable tie ordering."""
    found = []
    for transit_name in PLANETS:
        for natal_name in PLANETS:
            separation = abs(transit[transit_name]["lon"] - natal[natal_name]["lon"]) % 360
            separation = min(separation, 360 - separation)
            for name, (angle, max_orb, factor) in ASPECTS.items():
                orb = abs(separation - angle)
                if orb <= max_orb:
                    found.append({"transit": transit_name, "natal": natal_name, "aspect": name,
                                  "orb": round(orb, 2), "strength": round(factor * (1 - orb / max_orb), 3)})
    return sorted(found, key=lambda item: -item["strength"])


def branch_interactions(pillars, today_branch):
    """Order-independent pairs correct the original sorted-tuple mismatch."""
    found = []
    for name, pillar in pillars.items():
        pair = frozenset((pillar["branch"], today_branch))
        kind = "combination" if pair in _COMBINATIONS else "clash" if pair in _CLASHES else None
        if kind:
            found.append({"type": kind, "natal_pillar": name,
                          "natal_branch": pillar["branch"], "today_branch": today_branch})
    return found


def _saju_signals(result):
    analysis = result["analysis"]
    natal, today = analysis["natal"], analysis["today"]
    if natal["day_master"] is None:
        return None, [{"field": "day_master", "reason": "The birth-time range crosses a day-master boundary; the Saju reading is unavailable."}]
    pillars = {key: value for key, value in natal["pillars"].items() if value is not None}
    dist = dict.fromkeys(_ELEMENTS, 0)
    for pillar in pillars.values():
        dist[pillar["stem_info"]["element"]] += 1
        dist[pillar["branch_element"]] += 1
    current = today["pillars"]["day"]
    active = sorted({current["stem_info"]["element"], current["branch_element"]})
    omissions = []
    missing = [key for key, value in natal["pillars"].items() if value is None]
    if missing:
        omissions.append({"field": "natal.pillars", "reason": "Uncertain natal pillars are omitted: " + ", ".join(missing) + ". Element counts use confirmed pillars only."})
    return {"natal": {"day_master": {"stem": natal["day_master"]["text"]}},
            "today": {"pillar": {"stem": current["stem"]}},
            "signals": {"daily_ten_god": current["stem_ten_god"]["korean"],
                        "active_elements": active, "element_distribution": dist,
                        "interactions": branch_interactions(pillars, current["branch"]),
                        "deficient_elements_active": sorted(element for element in active if dist[element] == 0),
                        "excess_elements_active": sorted(element for element in active if dist[element] >= 3)}}, omissions


def _astrology_signals(result):
    analysis = result["analysis"]
    natal, today = analysis["natal"], analysis["today"]
    def positions(chart):
        points = {}
        for name in PLANETS:
            value = chart["planets"][name.title()]["longitude_deg"]
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Invalid planetary position.")
            points[name] = {"lon": round(value, 2)}
        return points
    natal_positions, transit_positions = positions(natal), positions(today)
    if transit_positions is None:
        raise ValueError("Current planetary positions are unavailable.")
    aspects = find_aspects(transit_positions, natal_positions) if natal_positions is not None else []
    active = sorted({today["planets"][name]["element"].lower() for name in ("Sun", "Moon")})
    omissions = []
    if natal_positions is None:
        omissions.append({"field": "transit_aspects", "reason": "Birth time is unknown. Personal transit aspects are omitted; the astrology reading uses today's Sun/Moon elements only."})
    return {"aspects": aspects, "signals": {"active_elements": active}}, omissions


def interpret_system(system, result):
    if not isinstance(result, dict) or not isinstance(result.get("analysis"), dict):
        raise ValueError("The factual analysis contract is unavailable.")
    raw, omissions = _saju_signals(result) if system == "saju" else _astrology_signals(result)
    if raw is None:
        return {"status": "unavailable", "event": None, "quality": None,
                "evidence": [], "source_street_weights": None, "routing_weights": None,
                "signals": {}, "omissions": omissions}
    interpreter = interpret_saju if system == "saju" else interpret_astrology
    accumulator = interpreter(raw, load_rules(CONFIG / f"{system}_rules.csv"))
    values = accumulator.result()
    weights = street_weights(values["quality"])
    return {"status": "ready", **values, "evidence": accumulator.evidence,
            "source_street_weights": weights, "routing_weights": routing_projection(weights),
            "signals": raw["signals"], "omissions": omissions,
            **({"interpreted_aspects": [item for item in raw["aspects"]
                                        if item["strength"] >= MIN_ASPECT_STRENGTH][:MAX_ASPECTS]}
               if system == "astrology" else {})}


def coverage():
    return {
        "supported": [
            {"source": "green", "target": "green", "label": "Greenery", "proxy": "Historical street-tree presence from 2015 inventory counts; no current vegetation or park measurement"},
            {"source": "quiet", "target": "quiet", "label": "Quiet", "proxy": "Street-environment quietness proxy; no live noise measurement"},
            {"source": "open_sky", "target": "open_sky", "label": "Openness", "proxy": "Built-form openness proxy; no measured sky-view factor"},
            {"source": "vibrant", "target": "vibrant", "label": "Street activity", "proxy": "Nearby place activity proxy; no live footfall measurement"},
        ],
        "omitted": [{"source": key, "label": label,
                     "reason": "No calibrated segment attribute is connected to the live routing graph; omitted from route cost, not scored as zero."}
                    for key, label in (("water", "Water"), ("sunlight", "Sunlight"), ("rest", "Rest"), ("culture", "Culture"))],
        "projection": "Keep the full eight source weights for explanation; select the four supported proxies and renormalize their sum to one.",
        "access": "Convenience remains a neutral 5/10 for nearby place suggestions; it is not an original street axis or a route-cost term.",
        "empirical_quality_verified": False,
    }


@lru_cache(maxsize=1)
def provenance():
    manifest = json.loads((CONFIG / "source_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        if sha256((CONFIG / item["file"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Original rule file checksum mismatch.")
    return {
        "origin": "route_maker/config and route_maker/engine/combine_reading.py",
        "rule_files": manifest["files"], "source_rules_unchanged": True,
        "pipeline": "Independent CSV signals → 3 events + 6 qualities → 1−exp(−score) per source → 50:50 mean → full 6×8 matrix → normalized street weights → supported GIS projection.",
        "rounding": "Source saturation: 3 decimals; source mean: 3 decimals; full street normalization: 4 decimals, matching the original. Supported projection has no additional rounding.",
        "experimental": True, "externally_validated": False,
        "source_verification_scope": "Verified rows document symbolic background, not scientific validation of numeric weights, predictions or route benefits.",
        "differences_from_original": [
            "Uses the existing lunar-python and Kerykeion/libephemeris factual providers. Approximate fallback calculations are disabled.",
            "Birth astrology uses the supplied time and timezone instead of an assumed UTC noon; today's reference is New York noon instead of UTC noon.",
            "Saju year/month boundaries use the same instant in UTC+08:00; day/hour pillars use birthplace civil time. Uncertain pillars are omitted.",
            "Unordered Saju branch pairs correct the original sorted-tuple mismatch, which could omit valid combinations or clashes.",
            "The original nine planets, aspect factors, orb limits, two-decimal position adapter, 0.35 strength threshold and top-six selection are retained; current provider positions can differ from the former astronomy-engine positions.",
            "Unknown birth time never invents noon: astrology transit aspects are omitted and current Sun/Moon element rules remain available.",
            "The original eight street weights are preserved. Only four connected GIS proxies influence the current real-street route.",
        ],
        "aspect_policy": {"planets": list(PLANETS), "aspects": {key: {"angle": value[0], "max_orb": value[1], "factor": value[2]} for key, value in ASPECTS.items()},
                          "minimum_strength": MIN_ASPECT_STRENGTH, "maximum_aspects": MAX_ASPECTS,
                          "natal_planet_factor": NATAL_PLANET_FACTOR},
    }


def combine_readings(saju, astrology):
    if any(source.get("status") != "ready" for source in (saju, astrology)):
        return None
    combined = combine_scores(saju, astrology)
    weights = street_weights(combined["quality"])
    return {
        "version": VERSION, "status": "ready", "saju": saju, "astrology": astrology,
        "combined": combined, "dominant_event": max(combined["event"], key=combined["event"].get),
        "top_qualities": sorted(combined["quality"], key=combined["quality"].get, reverse=True)[:2],
        "evidence": sorted(saju["evidence"] + astrology["evidence"], key=lambda item: -item["score"])[:8],
        "source_street_weights": weights, "routing_weights": routing_projection(weights),
        "coverage": coverage(), "provenance": provenance(),
    }


def compatibility_profile(reading, *, combined=False):
    """Display-only sliders; route cost consumes the unrounded projection."""
    if reading is None or reading.get("status") != "ready":
        return {"status": "unavailable", "profile_scores": None, "score_details": {}, "evidence": []}
    routes = reading["routing_weights"]
    peak = max(routes.values())
    mapping = {"open": "open_sky", "green": "green", "still": "quiet", "social": "vibrant"}
    values = {key: 1 + 9 * routes[axis] / peak for key, axis in mapping.items()}
    values["access"] = 5.
    rounded = {key: math.floor(value + .5) for key, value in values.items()}
    policy_id = FUSION_POLICY if combined else VERSION
    return {
        "status": "ready", "policy_id": policy_id, "profile_scores": rounded,
        "score_details": {key: {"unrounded": value, "rounded": rounded[key]} for key, value in values.items()},
        "routing_weights": routes, "source_street_weights": reading["source_street_weights"],
        "evidence": reading["evidence"],
        "policy": {"id": policy_id, "experimental": True, "externally_validated": False,
                   "stage": "after_independent_event_quality_before_street_matrix",
                   "system_weights": {"saju": .5, "astrology": .5} if combined else None,
                   "description": "Original CSV rules produce events and qualities. Source qualities combine equally before the eight-axis street matrix.",
                   "slider_display": "1 + 9 × supported weight / largest supported weight, then half-up rounding. Convenience is fixed at 5. These display scores do not control the automatic route.",
                   "route_cost": "segment_length × (1.6 − weighted sum of the four connected street proxies)",
                   "rounding": "Automatic route uses routing_weights without further rounding."},
    }
