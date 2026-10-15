"""Describe observations separately from scores, without changing routing costs.

Evidence status is a provenance label, never an accuracy probability. In
particular, a nearby record with a score of zero differs from no nearby record,
and absence from a source is not proof of absence on the street.
"""
from __future__ import annotations

import copy
import math
from collections import Counter
from typing import Any

import numpy as np
from scipy.spatial import cKDTree

METRICS = ("open", "green", "still", "social", "access")
STATUSES = {"crosschecked", "partial", "unverified"}
RAW_FIELDS = {"open": "open", "green": "trees_within_radius",
              "still": "major_road_distance_m", "social": "social_raw", "access": "access_raw"}
MEASUREMENTS = {
    "open": "Eight horizontal building-boundary rays; mean distance divided by radius",
    "green": "Historical street-tree records within the configured radius",
    "still": "Distance in metres to the nearest mapped major road",
    "social": "Sum of nearby place-type social components",
    "access": "Sum of nearby place-type access components",
}
LIMITATIONS = {
    "open": "Mapped footprints do not measure perceived openness, visibility, or building height.",
    "green": "Tree records do not measure current canopy, shade, or perceived greenness.",
    "still": "Major-road distance is a proxy; noise has not been measured.",
    "social": "Type-based scores do not establish atmosphere, availability, or completeness of places.",
    "access": "Type-based scores do not verify wheelchair access, hours, or physical entrances.",
}


def _source_entries(metadata: dict, key: str) -> list:
    source_files = metadata.get("source_files", {})
    source = source_files.get(key) if isinstance(source_files, dict) else None
    if source is not None:
        return [copy.deepcopy(source)]
    if key == "buildings":
        source = metadata.get("building_context_sources") or metadata.get("osm")
    elif key == "roads":
        source = metadata.get("osm")
    else:
        source = metadata.get(key)
    if isinstance(source, list):
        return copy.deepcopy(source)
    if isinstance(source, dict) and source.get("file"):
        return [copy.deepcopy(source)]
    return []


def _legacy_profile(dataset) -> dict:
    metadata = dataset.metadata
    sources = {key: _source_entries(metadata, key) for key in ("buildings", "trees", "roads", "places")}
    tree_info = metadata.get("trees", {})
    tree_source_text = str(sources["trees"]).lower()
    is_2015 = "2015" in tree_source_text or any("2015" in str(x) for x in metadata.get("limitations", []))
    tree_statuses = tree_info.get("included_status_counts", tree_info.get("status_counts", {}))
    # A single window containing only Alive records cannot prove an Alive-only
    # filter. Declare that policy only if the prepared metadata explicitly does.
    tree_policy = metadata.get("tree_policy", tree_info.get("policy", "unspecified"))
    all_statuses = any(str(key).lower() in {"dead", "stump"} and value for key, value in tree_statuses.items())
    fields = {"open": "buildings", "green": "trees", "still": "roads", "social": "places", "access": "places"}
    available = {"open": bool(dataset.buildings), "green": bool(dataset.trees) or bool(sources["trees"]),
                 "still": bool(dataset.major_roads), "social": bool(dataset.places) or bool(sources["places"]),
                 "access": bool(dataset.places) or bool(sources["places"])}
    if tree_info.get("provided") is False:
        available["green"] = False
    metrics = {}
    for name in METRICS:
        limitations = [LIMITATIONS[name], "Prepared source has not been independently crosschecked for this metric."]
        if name == "green":
            if is_2015:
                limitations.append("The source is the historical 2015 census, not a current inventory.")
            if all_statuses:
                limitations.append("This prepared dataset includes Dead and/or Stump records.")
            else:
                limitations.append(f"Prepared tree inclusion policy: {tree_policy}.")
        metrics[name] = {
            "measurement": MEASUREMENTS[name], "source_date": "2015" if name == "green" and is_2015 else None,
            "sources": sources[fields[name]], "source_available": available[name],
            "evidence_status": "partial" if sources[fields[name]] and available[name] else "unverified",
            "limitations": limitations,
        }
    return {"schema_version": 1, "profile_id": "legacy-provenance-fallback-v1", "metrics": metrics,
            "empirical_quality_verified": False, "metadata_origin": "inferred_from_prepared_source_metadata"}


def validate_evidence_profile(value: dict) -> dict:
    """Reject unsupported claims instead of silently treating them as verified."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Evidence metadata must be an object with schema_version 1")
    if not isinstance(value.get("profile_id"), str) or not value["profile_id"].strip():
        raise ValueError("Evidence metadata requires a profile_id")
    metrics = value.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(METRICS):
        raise ValueError("Evidence metadata must describe all five metrics")
    if value.get("empirical_quality_verified", False) is not False:
        raise ValueError("Empirical experience quality has not been verified by this evidence layer")
    result = copy.deepcopy(value)
    result["empirical_quality_verified"] = False
    result.setdefault("metadata_origin", "prepared_dataset")
    for name, item in result["metrics"].items():
        if not isinstance(item, dict):
            raise ValueError(f"Evidence metric {name} must be an object")
        if not isinstance(item.get("measurement"), str) or not item["measurement"].strip():
            raise ValueError(f"Evidence metric {name} requires a measurement description")
        if item.get("evidence_status") not in STATUSES:
            raise ValueError(f"Invalid evidence_status for {name}")
        if "source_date" not in item or (item["source_date"] is not None and not isinstance(item["source_date"], str)):
            raise ValueError(f"Evidence source_date for {name} must be a string or null")
        if not isinstance(item.get("sources"), list) or not all(isinstance(x, (str, dict)) for x in item["sources"]):
            raise ValueError(f"Evidence sources for {name} must be a list")
        if not isinstance(item.get("limitations"), list) or not all(isinstance(x, str) for x in item["limitations"]):
            raise ValueError(f"Evidence limitations for {name} must be a list of strings")
        if "source_available" in item and type(item["source_available"]) is not bool:
            raise ValueError(f"Evidence source_available for {name} must be boolean")
        if item.get("empirical_quality_verified", False) is not False:
            raise ValueError(f"Experience quality is not verified for {name}")
        if item["evidence_status"] == "crosschecked":
            if (item.get("verification_scope") != "record_identity_only"
                    or not isinstance(item.get("verification_source"), str)
                    or not item["verification_source"].strip()):
                raise ValueError("Crosschecked evidence requires record_identity_only scope and verification_source")
            if not item["sources"] or item.get("source_available") is False:
                raise ValueError("Crosschecked evidence requires available source records")
        item["empirical_quality_verified"] = False
    return result


def _nearby_indices(midpoints, points, radius: float) -> list:
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Evidence neighbourhood radius must be positive and finite")
    if not points:
        return [[] for _ in midpoints]
    return cKDTree(np.asarray(points, dtype=float)).query_ball_point(np.asarray(midpoints, dtype=float), radius)


def _identity_crosschecked(place: dict) -> bool:
    return (place.get("evidence_status") == "crosschecked"
            and place.get("verification_scope") == "record_identity_only"
            and isinstance(place.get("verification_source"), str)
            and bool(place["verification_source"].strip()))


class EvidenceIndex:
    """Cache source-record support for the exact prepared feature midpoints."""

    def __init__(self, dataset, features: dict):
        self.features = features["edges"]
        self.edge_ids = list(self.features)
        candidate = dataset.metadata.get("evidence")
        self.profile = validate_evidence_profile(candidate if candidate is not None else _legacy_profile(dataset))
        # Explicit false always takes priority. An empty neighbourhood with a
        # provided source is still a valid zero count in that particular source.
        fallback = _legacy_profile(dataset)
        for name, item in self.profile["metrics"].items():
            item.setdefault("source_available", fallback["metrics"][name]["source_available"])
        midpoints = [self.features[key]["midpoint_xy"] for key in self.edge_ids]
        radius = float(features["parameters"].get("poi_radius_m", 100.0))
        neighbours = _nearby_indices(midpoints, [(float(p["x"]), float(p["y"])) for p in dataset.places], radius)
        self.place_counts = {key: len(ids) for key, ids in zip(self.edge_ids, neighbours)}
        verified = [_identity_crosschecked(place) for place in dataset.places]
        self.crosschecked_place_counts = {key: sum(verified[i] for i in ids) for key, ids in zip(self.edge_ids, neighbours)}

    def metadata(self) -> dict:
        result = copy.deepcopy(self.profile)
        result.update({"affects_route_cost": False, "confidence_probability": None,
                       "missing_records_mean_absence": False,
                       "status_meaning": {"crosschecked": "Record identity checked against the named source only",
                                          "partial": "Source-backed proxy with incomplete coverage or validation",
                                          "unverified": "Adequate source provenance or independent verification unavailable"}})
        return result

    def edge(self, edge_id: str) -> dict:
        values = self.features[edge_id]
        result = {}
        for name, descriptor in self.profile["metrics"].items():
            available = descriptor["source_available"]
            raw = values.get(RAW_FIELDS[name])
            if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw)):
                raise ValueError(f"Non-finite or invalid raw evidence measurement: {name}")
            count = self.place_counts[edge_id] if name in ("social", "access") else (raw if name == "green" else None)
            if not available or raw is None:
                observation = "source_unavailable"
            elif count == 0:
                observation = "no_records_nearby"
            elif raw == 0:
                observation = "observed_zero"
            else:
                observation = "observed"
            item = {
                "model_score": values[name],
                "observed_score": values[name] if available and raw is not None else None,
                "raw_measurement": raw if available else None,
                "source_date": descriptor["source_date"], "evidence_status": descriptor["evidence_status"],
                "observation_status": observation, "empirical_quality_verified": False,
            }
            if count is not None:
                item["nearby_record_count"] = int(count) if available else None
            if name in ("social", "access"):
                item["crosschecked_place_records_nearby"] = self.crosschecked_place_counts[edge_id] if available else None
                item["crosscheck_scope"] = "record_identity_only; nearby matched food records do not validate the whole metric"
            result[name] = item
        return result

    def route(self, edge_ids: list[str]) -> dict:
        # Routing may traverse part of a source edge or return through it via a
        # stop. Count unique used source edges; do not invent length coverage.
        unique_ids = list(dict.fromkeys(edge_ids))
        missing = set(unique_ids) - set(self.features)
        if missing:
            raise ValueError("Route evidence refers to an unknown source edge")
        edge_values = [self.edge(edge_id) for edge_id in unique_ids]
        metrics = {}
        for name, descriptor in self.profile["metrics"].items():
            rows = [row[name] for row in edge_values]
            scores = [row["observed_score"] for row in rows if row["observed_score"] is not None]
            metrics[name] = {
                "measurement": descriptor["measurement"], "source_date": descriptor["source_date"],
                "sources": copy.deepcopy(descriptor["sources"]), "evidence_status": descriptor["evidence_status"],
                "observation_status_counts": dict(Counter(row["observation_status"] for row in rows)),
                "observed_score_range": [min(scores), max(scores)] if scores else None,
                "limitations": list(descriptor["limitations"]), "empirical_quality_verified": False,
            }
            if name in ("social", "access"):
                metrics[name]["edges_with_crosschecked_place_records"] = sum(
                    (row["crosschecked_place_records_nearby"] or 0) > 0 for row in rows)
        return {"schema_version": 1, "profile_id": self.profile["profile_id"],
                "unique_source_edges": len(unique_ids),
                "aggregation_basis": "unique traversed source edges; includes partially traversed edges; not length weighted",
                "metrics": metrics, "empirical_quality_verified": False,
                "affects_route_cost": False, "missing_records_mean_absence": False}
