"""Original route_maker cost applied to explicitly available street proxies.

Eight symbolic street weights are preserved in the profile response. Four have
usable indicators in this prepared dataset. Missing axes are excluded before
renormalization; absence of a measurement is never scored as zero.
"""
from __future__ import annotations

import math
from .scoring import number

POLICY_ID = "route-maker-proxy-v1"
MAPPING = {"green": "green", "quiet": "still", "open_sky": "open", "vibrant": "social"}
OMITTED = ["water", "sunlight", "rest", "culture"]
MEASUREMENTS = {
    "green": "Historical street-tree presence; not current canopy or shade.",
    "quiet": "Separation from major roads; not measured noise.",
    "open_sky": "Horizontal building clearance; not measured sky-view factor.",
    "vibrant": "Nearby place-type activity scores; not measured footfall or current atmosphere.",
}


def validate_reading_weights(value):
    if not isinstance(value, dict) or set(value) != set(MAPPING):
        raise ValueError("reading_weights requires the four supported source axes")
    result = {key: number(value[key], "reading_weights." + key, 0, 1) for key in MAPPING}
    total = math.fsum(result.values())
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("reading_weights must sum to one")
    return result


def evaluate_reading_edge(features, length_m, weights):
    length_m = number(length_m, "length_m", 0, 1e8)
    qualities = {axis: number(features[field], field, 0, 1) for axis, field in MAPPING.items()}
    fit = math.fsum(weights[axis] * qualities[axis] for axis in MAPPING)
    return {"cost": length_m * (1.6 - fit), "spatial_fit": fit}


def routing_policy(weights):
    return {"id": POLICY_ID, "mode": "original-reading-with-measured-proxies",
            "formula": "length_m * (1.6 - sum(weight * measured_proxy))",
            "reading_weights": dict(weights), "mapping": dict(MAPPING),
            "measurements": dict(MEASUREMENTS), "omitted_axes": list(OMITTED),
            "coverage": "Four available proxies; not the original eight-attribute dataset.",
            "openness_target_applied": False,
            "externally_validated": False}
