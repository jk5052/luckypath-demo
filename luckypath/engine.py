"""Apply today's inputs to a prepared city dataset and return map-ready results."""
from __future__ import annotations

import json
import hashlib
import math
from dataclasses import asdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, mapping
from shapely.ops import transform

from . import __version__
from .data import load_prepared_dataset
from .evidence import EvidenceIndex
from .calibration import validate_feature_calibration, validate_calibration_sources
from .routing import PreparedNetwork
from .scoring import Targets, Weights, evaluate_edge, number
from .reading_routing import validate_reading_weights, evaluate_reading_edge, routing_policy
from .travel_time import (parse_time_budget, estimate_route_time, max_network_length,
                          POLICY_ID as TIME_POLICY_ID)

TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)
TO_LONLAT = Transformer.from_crs(32618, 4326, always_xy=True)


class Engine:
    def __init__(self, data_path: str | Path):
        data_path = Path(data_path)
        self.dataset = load_prepared_dataset(data_path / "dataset.json")
        self.features = json.loads((data_path / "features.json").read_text(encoding="utf-8"))
        expected_digest = self.features.get("dataset_sha256")
        if expected_digest and expected_digest != hashlib.sha256((data_path / "dataset.json").read_bytes()).hexdigest():
            raise ValueError("Prepared dataset changed after feature generation; run prepare again")
        ids = {edge.id for edge in self.dataset.edges}
        if set(self.features.get("edges", {})) != ids:
            raise ValueError("Prepared features and road IDs differ; run prepare again")
        self.normalization = validate_feature_calibration(self.features)
        if "calibration" in self.features:
            validate_calibration_sources(self.dataset.metadata, self.features["calibration"])
        self.edge_by_id = {e.id: e for e in self.dataset.edges}
        self.evidence = EvidenceIndex(self.dataset, self.features)
        self.network = PreparedNetwork(self.dataset)
        self.distance_costs = {edge.id: edge.length for edge in self.dataset.edges}

    def recommend(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ValueError("Request must be a JSON object")
        allowed = {"origin", "destination", "via", "weights", "profile_scores", "reading_weights", "targets", "alpha", "max_snap_m", "lucky_radius_m", "lucky_limit", "time_budget"}
        unknown = set(request) - allowed
        if unknown:
            raise ValueError("Unknown request fields: " + ", ".join(sorted(unknown)))
        if "origin" not in request or "destination" not in request:
            raise ValueError("origin and destination are required [longitude, latitude] pairs")
        if "weights" in request and "profile_scores" in request:
            raise ValueError("Use weights OR profile_scores, not both")
        time_budget = (parse_time_budget(request["time_budget"], has_via=request.get("via") is not None)
                       if "time_budget" in request else None)
        weights = (Weights.from_profile_scores(request["profile_scores"])
                   if "profile_scores" in request else Weights.from_dict(request.get("weights")))
        targets = Targets.from_dict(request.get("targets"))
        alpha = number(request.get("alpha", 1.0), "alpha", 0, 10)
        reading_weights = (validate_reading_weights(request["reading_weights"])
                           if "reading_weights" in request else None)
        if reading_weights is not None and alpha != 1:
            raise ValueError("The original reading cost uses a fixed coefficient")
        max_snap = number(request.get("max_snap_m", 150), "max_snap_m", 0.01, 500)
        radius = number(request.get("lucky_radius_m", 1600), "lucky_radius_m", 1, 5000)
        limit = request.get("lucky_limit", 1)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 20:
            raise ValueError("lucky_limit must be an integer between 0 and 20")

        scored = {
            e.id: evaluate_edge(self.features["edges"][e.id], e.length, weights, targets, alpha)
            for e in self.dataset.edges
        }
        if reading_weights is not None:
            for edge in self.dataset.edges:
                scored[edge.id].update(evaluate_reading_edge(
                    self.features["edges"][edge.id], edge.length, reading_weights))
        costs = {key: value["cost"] for key, value in scored.items()}
        trip = self.network.prepare(
            origin_lonlat=request["origin"], destination_lonlat=request["destination"],
            via_lonlat=request.get("via"), max_snap_m=max_snap,
        )
        shortest = trip.plan_route(self.distance_costs)
        budget_result = None
        if time_budget is None:
            personalized = trip.plan_route(costs)
        else:
            shortest["time_estimate"] = estimate_route_time(shortest, time_budget)
            minimum = shortest["time_estimate"]["total_minutes"]
            cap = max_network_length(time_budget, shortest)
            # Comparison uses full precision; no displayed rounding can turn an
            # over-budget route into a feasible recommendation.
            if minimum > time_budget["max_minutes"]:
                personalized = None
            else:
                personalized = trip.plan_budget_route(costs, max_network_length_m=max(0., cap))
                personalized["time_estimate"] = estimate_route_time(personalized, time_budget)
                if personalized["time_estimate"]["total_minutes"] > time_budget["max_minutes"] + 1e-9:
                    raise ValueError("Time-budget calculation failed its final limit check")
            estimate = personalized["time_estimate"] if personalized else None
            budget_result = {
                "policy_id": TIME_POLICY_ID, **time_budget,
                "status": "feasible" if personalized else "infeasible",
                "minimum_required_minutes": minimum,
                "available_for_walking_minutes": time_budget["max_minutes"] - time_budget["stop_minutes"] - time_budget["buffer_minutes"],
                "estimated_total_minutes": estimate["total_minutes"] if estimate else None,
                "remaining_minutes": max(0., time_budget["max_minutes"] - estimate["total_minutes"]) if estimate else None,
                "selection": personalized.get("budget_selection") if personalized else None,
                "arrival_guaranteed": False,
                "reason": None if personalized else "minimum_estimated_time_exceeds_budget",
            }
        stops = self._lucky_stops(request["origin"], scored, radius, limit) if personalized else []
        if personalized:
            personalized["route"]["properties"].update(route_kind="personalized", label="오늘의 추천 경로")
        shortest["route"]["properties"].update(route_kind="shortest", label="거리 기준 최단 경로")
        for route in (personalized, shortest):
            if route is None:
                continue
            route["evidence"] = self.evidence.route(route["edge_ids"])
            route["route"]["properties"].update(normalization_id=self.normalization["id"],
                                               normalization_method=self.normalization["method"],
                                               evidence=route["evidence"])
            if time_budget:
                route["route"]["properties"]["time_estimate"] = route["time_estimate"]
                route["route"]["properties"]["within_time_budget"] = route["time_estimate"]["total_minutes"] <= time_budget["max_minutes"] + 1e-9
        result = {
            "schema_version": 2 if time_budget else 1,
            "model_version": f"luckypath-python-{__version__}",
            "normalization": self.normalization,
            "evidence": self.evidence.metadata(),
            "weights": weights.to_dict(), "targets": asdict(targets), "alpha": alpha,
            "input_scale": "1..10 mapped by (x-1)/9" if "profile_scores" in request else "native 0..1",
            "personalized": personalized,
            "shortest": shortest,
            "extra_distance_m": personalized["length_m"] - shortest["length_m"] if personalized else None,
            "lucky_stops": stops,
            "lucky_stop_auto_added": False,
            "explicit_via": request.get("via"),
            "geojson": {"type": "FeatureCollection", "normalization": self.normalization,
                        "features": [shortest["route"], *([personalized["route"]] if personalized else []), *[s["feature"] for s in stops]]},
            "notes": [
                "Social/access weights rank candidate stop points; they do not change the active GH route-cost formula.",
                "Lucky stops are not automatically visited. Send a selected point as via to explicitly route through it.",
                "Source formulas were inspected; complete Rhino/Grasshopper output parity is not yet verified.",
                "Distances follow the static OSM network. Stillness is a major-road-distance proxy, not measured noise.",
            ],
        }
        if budget_result:
            result["budget"] = budget_result
            result["geojson"]["budget"] = budget_result
            result["notes"].extend([
                "Time estimates use the selected path, configured speed, straight snap links, stop duration and a fixed buffer; they are not arrival guarantees.",
                "A budgeted route is the lowest original cost among a bounded candidate search; global constrained optimality is not claimed.",
                "Snap links are estimated approaches, not verified walkable entrance paths. The buffer is not measured traffic or signal delay.",
            ])
        if reading_weights is not None:
            policy = routing_policy(reading_weights)
            result["routing_policy"] = policy
            result["geojson"]["routing_policy"] = policy
            result["notes"][0] = (
                "The original reading cost uses four supported street proxies, including place-type activity. "
                "Water, sunlight, rest and culture are omitted because their required measurements are unavailable. "
                "Five rounded sliders are a display/manual control; automatic route costs use the precise reading weights.")
            for route in (personalized, shortest):
                if route is not None:
                    route["route"]["properties"]["routing_policy"] = policy
        return result

    def _lucky_stops(self, origin, scored, radius, limit):
        if not limit:
            return []
        x, y = TO_METRES.transform(*origin)
        choices = []
        for edge in self.dataset.edges:
            score = scored[edge.id]["potential_fit"]
            if score is None:
                continue
            xy = self.features["edges"][edge.id]["midpoint_xy"]
            distance = math.dist((x, y), xy)
            if distance < radius:
                choices.append((-score, distance, edge.id, xy))
        # Added deterministic tie-break: nearer midpoint, then stable OSM edge ID.
        choices.sort(key=lambda row: row[:3])
        result = []
        for negative_score, distance, edge_id, xy in choices[:limit]:
            lonlat = list(TO_LONLAT.transform(*xy))
            properties = {
                "kind": "lucky_stop_candidate", "rank": len(result) + 1,
                "edge_id": edge_id, "potential_fit": -negative_score,
                "straight_line_from_origin_m": distance,
                "label": f"추천 정차 후보 {len(result) + 1}",
                "is_venue": False, "route_access_verified": False,
                "evidence": self.evidence.edge(edge_id),
            }
            result.append({**properties, "lonlat": lonlat,
                           "feature": {"type": "Feature", "geometry": {"type": "Point", "coordinates": lonlat}, "properties": properties}})
        return result

    def metadata(self):
        return {**self.dataset.metadata,
                "normalization": self.normalization,
                "evidence": self.evidence.metadata(),
                "normalization_scope": self.features["parameters"].get(
                    "normalization_scope", "current prepared dataset; not comparable across windows"),
                "feature_parameters": self.features["parameters"],
                "defaults": {"weights": Weights().to_dict(), "targets": asdict(Targets()), "alpha": 1},
                "weights_affecting_route": ["open", "green", "still"],
                "weights_affecting_stops": ["social", "access"],
                "layers": ["roads", "places", "waterfront", "buildings", "trees"]}

    def layer(self, name):
        features = []
        if name == "roads":
            for edge in self.dataset.edges:
                values = self.features["edges"][edge.id]
                properties = {"edge_id": edge.id, "length_m": edge.length, "highway": edge.osm_tags.get("highway"),
                              **{key: values[key] for key in ("open", "green", "still", "social", "access")},
                              "evidence": self.evidence.edge(edge.id)}
                features.append({"type": "Feature", "properties": properties,
                                 "geometry": mapping(transform(TO_LONLAT.transform, LineString(edge.coords)))})
        elif name == "places":
            for p in self.dataset.places:
                properties = {k: v for k, v in p.items() if k not in ("x", "y", "latitude", "longitude")}
                features.append({"type": "Feature", "properties": properties,
                                 "geometry": {"type": "Point", "coordinates": [float(p["longitude"]), float(p["latitude"])]}})
        elif name in ("waterfront", "buildings"):
            for i, geom in enumerate(getattr(self.dataset, name)):
                features.append({"type": "Feature", "properties": {"index": i},
                                 "geometry": mapping(transform(TO_LONLAT.transform, geom))})
        elif name == "trees":
            for i, xy in enumerate(self.dataset.trees):
                features.append({"type": "Feature", "properties": {"index": i},
                                 "geometry": {"type": "Point", "coordinates": list(TO_LONLAT.transform(*xy))}})
        else:
            raise ValueError("Unknown map layer")
        return {"type": "FeatureCollection", "features": features}
