"""Explicit local walking-time assumptions, independent of experience scores.

Time is an estimate on the actual selected geometry, not a live arrival promise.
Straight snap connectors are accounted for but are not verified entrances.
"""
from __future__ import annotations

import math

POLICY_ID = "walking-time-budget-v1"
DEFAULTS = {"max_minutes": 30, "walking_speed_kmh": 4.5,
            "buffer_minutes": 3, "stop_minutes": 0}
LIMITS = {"max_minutes": (1, 240), "walking_speed_kmh": (2, 6),
          "buffer_minutes": (0, 30), "stop_minutes": (0, 120)}


def parse_time_budget(value, *, has_via=False):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("time_budget must contain only max_minutes, walking_speed_kmh, buffer_minutes, stop_minutes")
    if "max_minutes" not in value:
        raise ValueError("time_budget.max_minutes is required")
    result = {}
    for key, (low, high) in LIMITS.items():
        raw = value.get(key, DEFAULTS[key])
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"time_budget.{key} must be a number between {low} and {high}")
        try:
            number = float(raw)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"time_budget.{key} is outside the supported range") from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"time_budget.{key} must be finite and between {low} and {high}")
        result[key] = number
    if result["stop_minutes"] > 0 and not has_via:
        raise ValueError("time_budget.stop_minutes requires a via point")
    return result


def connector_distance(route):
    """Enter/exit the network per nonzero requested leg; a stop is a return link.

This also handles via=origin/destination and origin=destination without
counting a trip from a point back to that identical point as extra walking.
"""
    snaps = route["snaps"]
    labels = ["origin"] + (["via"] if "via" in snaps else []) + ["destination"]
    distances = []
    for before, after in zip(labels, labels[1:]):
        start, end = snaps[before], snaps[after]
        if start["input_lonlat"] == end["input_lonlat"]:
            continue
        distances.extend((start["distance_m"], end["distance_m"]))
    return math.fsum(distances)


def estimate_route_time(route, budget):
    metres_per_minute = budget["walking_speed_kmh"] * 1000 / 60
    network = route["length_m"]
    connectors = connector_distance(route)
    walking = network / metres_per_minute
    approach = connectors / metres_per_minute
    total = math.fsum((walking, approach, budget["stop_minutes"], budget["buffer_minutes"]))
    return {
        "policy_id": POLICY_ID, "estimated": True,
        "walking_speed_kmh": budget["walking_speed_kmh"],
        "network_distance_m": network, "connector_distance_m": connectors,
        "walking_minutes": walking, "connector_minutes": approach,
        "stop_minutes": budget["stop_minutes"], "buffer_minutes": budget["buffer_minutes"],
        "total_minutes": total,
        "connector_basis": "straight snap links for each nonzero leg; middle stop link traversed twice",
        "connectors_access_verified": False,
        "live_delays_included": False, "empirically_calibrated": False,
    }


def max_network_length(budget, route):
    metres_per_minute = budget["walking_speed_kmh"] * 1000 / 60
    return ((budget["max_minutes"] - budget["stop_minutes"] - budget["buffer_minutes"])
            * metres_per_minute - connector_distance(route))


def config():
    return {"policy_id": POLICY_ID, "defaults": dict(DEFAULTS),
            "limits": {key: {"min": value[0], "max": value[1]} for key, value in LIMITS.items()},
            "estimated": True, "external_api_required": False,
            "arrival_guaranteed": False, "empirically_calibrated": False,
            "includes_connectors": True, "connectors_access_verified": False}
