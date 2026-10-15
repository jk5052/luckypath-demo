"""The active, connected Grasshopper formulas, isolated from geometry."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

WEIGHT_KEYS = ("open", "green", "still", "social", "access")


def number(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{name} must be finite and between {lo} and {hi}")
    return value


@dataclass(frozen=True)
class Weights:
    open: float = 0.25
    green: float = 0.8
    still: float = 0.05
    social: float = 1.0
    access: float = 1.0

    def __post_init__(self):
        for key in WEIGHT_KEYS:
            number(getattr(self, key), f"weights.{key}", 0, 1)

    @classmethod
    def from_dict(cls, data=None):
        if data is None:
            return cls()
        if not isinstance(data, dict) or set(data) - set(WEIGHT_KEYS):
            raise ValueError(f"weights must contain only {', '.join(WEIGHT_KEYS)}")
        return cls(**{key: number(value, f"weights.{key}", 0, 1) for key, value in data.items()})

    @classmethod
    def from_profile_scores(cls, data):
        """New explicit adapter: 1 means zero weight, 10 means full weight.

        This adapter is not an astrology interpretation rule in the GH file.
        Require all five values to avoid mixing scales or implicit defaults.
        """
        if not isinstance(data, dict) or set(data) != set(WEIGHT_KEYS):
            raise ValueError("profile_scores requires all five keys: open, green, still, social, access")
        return cls(**{key: (number(data[key], f"profile_scores.{key}", 1, 10) - 1) / 9 for key in WEIGHT_KEYS})

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Targets:
    open: float = 0.8
    social: float = 0.8
    access: float = 0.8

    def __post_init__(self):
        for key in ("open", "social", "access"):
            number(getattr(self, key), f"targets.{key}", 0, 1)

    @classmethod
    def from_dict(cls, data=None):
        if data is None:
            return cls()
        if not isinstance(data, dict) or set(data) - {"open", "social", "access"}:
            raise ValueError("targets allows only open, social, access")
        return cls(**{key: number(value, f"targets.{key}", 0, 1) for key, value in data.items()})


def evaluate_edge(features, length_m, weights: Weights, targets: Targets, alpha=1.0):
    length_m = number(length_m, "length_m", 0, 1e8)
    alpha = number(alpha, "alpha", 0, 10)
    raw = {key: number(features[key], key, 0, 1) for key in WEIGHT_KEYS}
    fits = {
        "open": 1 - abs(raw["open"] - targets.open),
        "green": raw["green"],
        "still": raw["still"],
        "social": 1 - abs(raw["social"] - targets.social),
        "access": 1 - abs(raw["access"] - targets.access),
    }
    spatial_total = weights.open + weights.green + weights.still
    potential_total = weights.social + weights.access
    spatial_fit = sum(getattr(weights, k) * fits[k] for k in ("open", "green", "still")) / spatial_total if spatial_total else None
    potential_fit = sum(getattr(weights, k) * fits[k] for k in ("social", "access")) / potential_total if potential_total else None
    cost = length_m * (1 + alpha * (1 - spatial_fit)) if spatial_fit is not None else length_m
    return {"cost": cost, "spatial_fit": spatial_fit, "potential_fit": potential_fit, "fits": fits}
