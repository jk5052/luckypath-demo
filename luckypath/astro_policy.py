"""Versioned symbolic walking-profile policy, separate from chart calculation.

The mappings and blend weights below are application design choices. They are
not recovered Grasshopper formulas, traditional interpretive rules, validated
personality measurements, or probabilities.
"""
from __future__ import annotations

import math
from fractions import Fraction
from types import MappingProxyType

from .scoring import WEIGHT_KEYS


POLICY_ID = "saju-zodiac-symbolic-v1"
BASE_WEIGHTS = MappingProxyType({
    "natal_saju": Fraction(35, 100),
    "natal_zodiac": Fraction(35, 100),
    "daily_saju": Fraction(15, 100),
    "daily_zodiac": Fraction(15, 100),
})
# Tuple order is open, green, still, social, access.
SAJU_VECTORS = MappingProxyType({
    "wood": (6, 10, 5, 6, 4),
    "fire": (8, 4, 2, 10, 6),
    "earth": (5, 7, 7, 6, 8),
    "metal": (9, 3, 7, 4, 9),
    "water": (6, 7, 10, 3, 5),
})
ZODIAC_VECTORS = MappingProxyType({
    "fire": (8, 4, 2, 10, 6),
    "earth": (5, 9, 7, 5, 9),
    "air": (10, 4, 4, 9, 7),
    "water": (5, 7, 10, 4, 5),
})
COMPONENT_LABELS = {
    "natal_saju": "출생 사주",
    "natal_zodiac": "출생 별자리",
    "daily_saju": "선택한 날의 일주",
    "daily_zodiac": "선택한 날의 별자리",
}


def _fraction(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise ValueError(f"{label} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value if isinstance(value, Fraction) else Fraction(str(value))


def validate_policy(saju_vectors=None, zodiac_vectors=None, weights=None):
    """Validate a proposed mapping's shape and scale, not its real-world truth."""
    tables = (
        (SAJU_VECTORS if saju_vectors is None else saju_vectors,
         {"wood", "fire", "earth", "metal", "water"}, "saju"),
        (ZODIAC_VECTORS if zodiac_vectors is None else zodiac_vectors,
         {"fire", "earth", "air", "water"}, "zodiac"),
    )
    for table, elements, label in tables:
        if not hasattr(table, "keys") or set(table) != elements:
            raise ValueError(f"{label} must contain exactly {sorted(elements)}")
        for element, vector in table.items():
            if not isinstance(vector, (tuple, list)) or len(vector) != len(WEIGHT_KEYS):
                raise ValueError(f"{label}.{element} must contain five scores")
            for key, value in zip(WEIGHT_KEYS, vector):
                if not 1 <= _fraction(value, f"{label}.{element}.{key}") <= 10:
                    raise ValueError(f"{label}.{element}.{key} must be between 1 and 10")
    weights = BASE_WEIGHTS if weights is None else weights
    if not hasattr(weights, "keys") or set(weights) != set(BASE_WEIGHTS):
        raise ValueError("weights must contain all four components")
    fractions = [_fraction(value, key) for key, value in weights.items()]
    if any(value <= 0 for value in fractions) or sum(fractions) != 1:
        raise ValueError("component weights must be positive and sum to one")


def _vectors_as_dict(table):
    return {element: dict(zip(WEIGHT_KEYS, vector)) for element, vector in table.items()}


def policy_definition():
    """Return a fresh JSON-compatible copy for inspection and documentation."""
    return {
        "id": POLICY_ID,
        "kind": "application_design_policy",
        "externally_validated": False,
        "description": "사주·별자리의 상징을 반영한 산책 중요도 제안",
        "score_keys": list(WEIGHT_KEYS),
        "base_weights": {key: float(value) for key, value in BASE_WEIGHTS.items()},
        "component_labels": dict(COMPONENT_LABELS),
        "saju_vectors": _vectors_as_dict(SAJU_VECTORS),
        "zodiac_vectors": _vectors_as_dict(ZODIAC_VECTORS),
        "within_component": {
            "natal_saju": "각 사용 가능한 천간·지지의 주오행을 동일 비중으로 평균",
            "daily_saju": "선택한 날 일주의 천간·지지 주오행을 동일 비중으로 평균",
            "natal_zodiac": "사용 가능한 태양·달을 동일 비중으로 평균",
            "daily_zodiac": "사용 가능한 태양·달을 동일 비중으로 평균",
        },
        "missing_data_policy": (
            "누락 신호를 제외하고 구성 요소 안에서 다시 평균합니다. "
            "구성 요소 전체가 없으면 나머지 구성 요소의 비중을 다시 정규화합니다. "
            "모든 구성 요소가 없으면 점수를 만들지 않습니다."
        ),
        "rounding": "nearest integer, half up; exact rational arithmetic before rounding",
        "score_meaning": (
            "1은 해당 공간 기준 제외, 10은 높은 중요도입니다. 성격·운세의 확률이나 "
            "필요량이 아니며, 공간 목표값과 중요도는 별개입니다."
        ),
        "route_targets": {"open": 0.8, "social": 0.8, "access": 0.8},
    }


def _check_chart(chart, label):
    if chart is not None and not isinstance(chart, dict):
        raise ValueError(f"{label} must be a chart object or null")
    return chart or {}


def _component(chart, label, kind, pillar_keys=()):
    signals, missing = [], []
    table = SAJU_VECTORS if kind == "saju" else ZODIAC_VECTORS

    def add(element, field):
        if element is None:
            missing.append({"field": field, "reason": "계산에서 제공되지 않은 신호는 평균에서 제외했습니다."})
            return
        if not isinstance(element, str) or element not in table:
            raise ValueError(f"{field} has an unsupported {kind} element: {element!r}")
        signals.append({"field": field, "element": element,
                        "vector": dict(zip(WEIGHT_KEYS, table[element]))})

    if kind == "saju":
        pillars = chart.get("pillars")
        if pillars is not None and not isinstance(pillars, dict):
            raise ValueError(f"{label}.pillars must be an object or null")
        pillars = pillars or {}
        for name in pillar_keys:
            pillar = pillars.get(name)
            if pillar is not None and not isinstance(pillar, dict):
                raise ValueError(f"{label}.{name} must be a pillar object or null")
            if pillar is None:
                missing.append({"field": f"{label}.{name}",
                                "reason": "누락되거나 확정되지 않은 기둥은 평균에서 제외했습니다."})
                continue
            for part in ("stem", "branch"):
                add(pillar.get(f"{part}_element"), f"{label}.{name}.{part}")
    else:
        for name in ("sun", "moon"):
            body = chart.get(name)
            if body is not None and not isinstance(body, dict):
                raise ValueError(f"{label}.{name} must be a body object or null")
            add(body.get("element") if body is not None else None, f"{label}.{name}")

    mean = None
    if signals:
        mean = {key: sum(Fraction(signal["vector"][key]) for signal in signals) / len(signals)
                for key in WEIGHT_KEYS}
    return mean, {"available": bool(signals), "signal_count": len(signals),
                  "signals": signals, "missing_signals": missing}


def _round_half_up(value):
    integer, remainder = divmod(value.numerator, value.denominator)
    return integer + int(2 * remainder >= value.denominator)


def interpret_profile(natal, today):
    """Apply the v1 design policy to already calculated chart facts.

    Chart shape: ``pillars.{year,month,day,hour}.{stem,branch}_element``
    plus ``sun.element`` and ``moon.element``. Missing charts, pillars, bodies,
    or elements are excluded explicitly. Unknown non-null elements are errors.
    The chart calculator, not this function, resolves calendars and birth-time
    uncertainty. In particular, alternatives are never chosen implicitly here.
    """
    natal, today = _check_chart(natal, "natal"), _check_chart(today, "today")
    specs = {
        "natal_saju": (natal, "natal", "saju", ("year", "month", "day", "hour")),
        "natal_zodiac": (natal, "natal", "zodiac", ()),
        "daily_saju": (today, "today", "saju", ("day",)),
        "daily_zodiac": (today, "today", "zodiac", ()),
    }
    means, components, omissions = {}, {}, []
    for name, arguments in specs.items():
        means[name], components[name] = _component(*arguments)
        omissions.extend(components[name]["missing_signals"])
        if means[name] is None:
            omissions.append({"field": name,
                              "reason": "구성 요소 전체가 없어 제외하고 나머지 비중을 다시 정규화했습니다."})
    present_weight = sum(BASE_WEIGHTS[name] for name in BASE_WEIGHTS if means[name] is not None)
    if not present_weight:
        raise ValueError("No available chart signals; cannot generate profile scores")
    effective = {name: BASE_WEIGHTS[name] / present_weight if means[name] is not None else Fraction(0)
                 for name in BASE_WEIGHTS}
    scores, details = {}, {}
    for key in WEIGHT_KEYS:
        contributions, total = {}, Fraction(0)
        for name in BASE_WEIGHTS:
            mean = means[name][key] if means[name] is not None else None
            contribution = mean * effective[name] if mean is not None else Fraction(0)
            total += contribution
            contributions[name] = {
                "mean": float(mean) if mean is not None else None,
                "base_weight": float(BASE_WEIGHTS[name]),
                "effective_weight": float(effective[name]),
                "weighted_value": float(contribution),
            }
        scores[key] = _round_half_up(total)
        details[key] = {"unrounded": float(total), "rounded": scores[key],
                        "contributions": contributions}
    policy = policy_definition()
    policy["effective_weights"] = {key: float(value) for key, value in effective.items()}
    policy["missing_components"] = [name for name in BASE_WEIGHTS if means[name] is None]
    return {"profile_scores": scores, "score_details": details,
            "component_details": components, "policy": policy, "omissions": omissions}


validate_policy()
