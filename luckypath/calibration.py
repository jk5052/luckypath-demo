"""Versioned, fixed reference scales for comparisons across map windows.

Reference caps are a design policy, not evidence of experience or astrological
validity. This module applies a supplied reference; it does not fit caps to the
current window. Changing source context can still change raw measurements.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

METHOD = "fixed-positive-p95-linear-v1"
CRS = "EPSG:32618"
RADII = {"tree_radius_m": 50.0, "poi_radius_m": 100.0, "open_radius_m": 200.0}
RAW_FIELDS = {
    "green": "trees_within_radius", "social": "social_raw",
    "access": "access_raw", "still": "major_road_distance_m",
}
SOURCE_KEYS = {"roads", "trees", "places", "boroughs", "poi_mapping", "poi_rubric"}


def _finite_number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def calibration_digest(calibration: dict) -> str:
    """SHA-256 of canonical JSON, independent of source file indentation/order."""
    try:
        content = json.dumps(calibration, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Calibration must contain only finite JSON values") from exc
    return hashlib.sha256(content).hexdigest()


def validate_calibration(calibration: dict, parameters: dict | None = None) -> dict:
    """Validate the v1 measurement contract and optionally feature-cache radii."""
    if not isinstance(calibration, dict):
        raise ValueError("Calibration must be a JSON object")
    if type(calibration.get("schema_version")) is not int or calibration["schema_version"] != 1:
        raise ValueError("Unsupported calibration schema_version")
    if not isinstance(calibration.get("id"), str) or not calibration["id"].strip():
        raise ValueError("Calibration id must be a nonempty string")
    if calibration.get("method") != METHOD or calibration.get("crs") != CRS:
        raise ValueError("Unsupported calibration method or coordinate system")
    reference_parameters = calibration.get("parameters")
    if not isinstance(reference_parameters, dict) or set(reference_parameters) != set(RADII):
        raise ValueError("Calibration parameters must specify exactly the three measurement radii")
    for name, required in RADII.items():
        if _finite_number(reference_parameters[name], f"Calibration {name}", positive=True) != required:
            raise ValueError(f"Calibration {name} must equal {required:g} m for this method")
        if parameters is not None:
            if not isinstance(parameters, dict) or name not in parameters:
                raise ValueError(f"Feature cache is missing measurement parameter {name}")
            if _finite_number(parameters[name], f"Feature {name}", positive=True) != required:
                raise ValueError(f"Feature {name} does not match calibration; recompute raw features")
    metrics = calibration.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(RAW_FIELDS):
        raise ValueError("Calibration metrics must specify exactly green, social, access, and still")
    for metric, raw_field in RAW_FIELDS.items():
        rule = metrics[metric]
        if not isinstance(rule, dict) or rule.get("raw_field") != raw_field:
            raise ValueError(f"Calibration {metric} has an incompatible raw_field")
        if _finite_number(rule.get("lower"), f"Calibration {metric}.lower") != 0:
            raise ValueError(f"Calibration {metric}.lower must equal zero")
        _finite_number(rule.get("upper"), f"Calibration {metric}.upper", positive=True)
    calibration_digest(calibration)
    return calibration


def normalization_metadata(calibration: dict) -> dict:
    """Compact audit fields; full provenance is retained in features.calibration."""
    validate_calibration(calibration)
    return {
        "id": calibration["id"], "method": METHOD,
        "sha256": calibration_digest(calibration), "crs": CRS,
        "caps": {name: float(calibration["metrics"][name]["upper"]) for name in RAW_FIELDS},
        "parameters": dict(RADII),
        "comparable_across_windows": True,
        "comparison_condition": "same calibration reference, source snapshot, and measurement radii",
    }


def validate_calibration_sources(dataset_metadata: dict, calibration: dict) -> None:
    """Require the dataset loader's verified source binding before using NYC caps.

    The pure recalibration helper operates on numbers only; a runnable dataset
    must additionally demonstrate which collection supplied those numbers.
    """
    provenance = calibration.get("provenance")
    expected = provenance.get("source_sha256") if isinstance(provenance, dict) else None
    if (not isinstance(expected, dict) or set(expected) != SOURCE_KEYS
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                   for v in expected.values())):
        raise ValueError("Calibration provenance must bind all six NYC source SHA-256 digests")
    actual = dataset_metadata.get("calibration_source_sha256") if isinstance(dataset_metadata, dict) else None
    if actual != expected:
        raise ValueError("Dataset sources do not match calibration; prepare using its verified NYC source collection")


def _score(raw, upper, label):
    return min(1.0, _finite_number(raw, label) / upper)


def recalibrate_features(features: dict, calibration: dict) -> dict:
    """Return a recalibrated copy, without recomputing geometry or changing raw data.

    Source radii and all required raw measurements must be present. The input
    cache, its dataset digest, openness, and other measurements are preserved.
    """
    if (not isinstance(features, dict) or type(features.get("schema_version")) is not int
            or features["schema_version"] != 1):
        raise ValueError("Unsupported prepared feature schema")
    if not isinstance(features.get("parameters"), dict):
        raise ValueError("Feature cache is missing measurement parameters")
    validate_calibration(calibration, features["parameters"])
    if not isinstance(features.get("edges"), dict) or not features["edges"]:
        raise ValueError("Feature cache must contain road edges")
    result = copy.deepcopy(features)
    for edge_id, values in result["edges"].items():
        if not isinstance(values, dict):
            raise ValueError(f"Invalid cached measurements for edge {edge_id}")
        for metric, raw_field in RAW_FIELDS.items():
            values[metric] = _score(values.get(raw_field), calibration["metrics"][metric]["upper"],
                                    f"Edge {edge_id} {raw_field}")
    result["parameters"]["normalization"] = METHOD
    result["parameters"]["normalization_scope"] = f"fixed NYC calibration: {calibration['id']}"
    result["parameters"]["stillness"] = "fixed scale of distance to major roads; not measured noise"
    result["calibration"] = copy.deepcopy(calibration)
    result["normalization"] = normalization_metadata(calibration)
    return result


def validate_feature_calibration(features: dict) -> dict:
    """Audit a cache's reference, radii, digest, and scores before routing."""
    calibration = features.get("calibration")
    if calibration is None:
        if "normalization" in features or features.get("parameters", {}).get("normalization") == METHOD:
            raise ValueError("Feature normalization metadata has no calibration reference")
        return {"id": "legacy-window-relative-v1", "method": "window-minmax-and-tie-percentile-v1",
                "comparable_across_windows": False}
    if not isinstance(features.get("parameters"), dict):
        raise ValueError("Feature cache is missing measurement parameters")
    validate_calibration(calibration, features["parameters"])
    expected = normalization_metadata(calibration)
    if features.get("normalization") != expected or features["parameters"].get("normalization") != METHOD:
        raise ValueError("Feature normalization metadata does not match its calibration reference")
    for edge_id, values in features.get("edges", {}).items():
        for metric, raw_field in RAW_FIELDS.items():
            expected_score = _score(values.get(raw_field), expected["caps"][metric],
                                    f"Edge {edge_id} {raw_field}")
            actual = _finite_number(values.get(metric), f"Edge {edge_id} {metric}")
            if not math.isclose(actual, expected_score, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"Cached {metric} score does not match calibration for edge {edge_id}")
    return copy.deepcopy(expected)
