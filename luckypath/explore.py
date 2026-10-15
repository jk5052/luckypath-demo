"""Local map adapter: bounded inputs, honest route comparisons and real places.

This layer does not change the migrated Grasshopper cost formula. A venue's
ranking uses the existing social/access fit on its closest mapped road, not a
review score or a claim that its entrance is reachable or currently open.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from .engine import TO_METRES
from .scoring import Targets, Weights, evaluate_edge
from .travel_time import config as time_config
from .external_services import ExternalServices


VISIT_CATEGORIES = frozenset({
    "park", "plaza", "cafe", "restaurant", "bakery", "ice_cream_shop",
    "book_store", "museum", "art_gallery", "performing_arts_theater",
    "movie_theater", "community_center", "market", "gift_shop", "florist",
})
MODEL_METRICS = ("open", "green", "still")
RAW_METRICS = ("trees_within_radius", "major_road_distance_m")
ASSET_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "model.js": "text/javascript; charset=utf-8",
    "profile.js": "text/javascript; charset=utf-8",
    "external.js": "text/javascript; charset=utf-8",
    "vendor/leaflet.js": "text/javascript; charset=utf-8",
    "vendor/leaflet.css": "text/css; charset=utf-8",
    "vendor/LICENSE": "text/plain; charset=utf-8",
    "vendor/images/layers.png": "image/png",
    "vendor/images/layers-2x.png": "image/png",
    "vendor/images/marker-icon.png": "image/png",
    "vendor/images/marker-icon-2x.png": "image/png",
    "vendor/images/marker-shadow.png": "image/png",
}


def _coordinate(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(item) for item in value)):
        raise ValueError(f"{label} must be two finite numbers [longitude, latitude]")
    return [float(item) for item in value]


def _normalized_name(value):
    return re.sub(r"[^\w]+", "", str(value).casefold())


class ExploreService:
    def __init__(self, engine, data_path, *, external_services=None):
        self.engine = engine
        self.data_path = Path(data_path)
        bbox = engine.dataset.metadata.get("study_bbox_wgs84")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or any(isinstance(n, bool) or not isinstance(n, (int, float))
                       or not math.isfinite(n) for n in bbox)
                or not -180 <= bbox[0] < bbox[2] <= 180
                or not -90 <= bbox[1] < bbox[3] <= 90):
            raise ValueError("Explore requires a prepared study_bbox_wgs84")
        self.bbox = bbox
        self.external = external_services if external_services is not None else ExternalServices(self.bbox)
        request_path = self.data_path / "green-request.json"
        if not request_path.is_file():
            raise ValueError("Explore requires a saved green-request.json beside the prepared dataset")
        self.initial_request = json.loads(request_path.read_text(encoding="utf-8"))
        self._validate_endpoints(self.initial_request)
        self._roads = [LineString(edge.coords) for edge in engine.dataset.edges]
        self._road_tree = STRtree(self._roads)
        self._places = self._eligible_places()
        self._context = None

    def _inside(self, coordinate):
        return (self.bbox[0] <= coordinate[0] <= self.bbox[2]
                and self.bbox[1] <= coordinate[1] <= self.bbox[3])

    def _validate_endpoints(self, request):
        if not isinstance(request, dict):
            raise ValueError("Request must be a JSON object")
        for label in ("origin", "destination", "via"):
            if label == "via" and request.get(label) is None:
                continue
            value = _coordinate(request.get(label), label)
            if not self._inside(value):
                raise ValueError(f"{label} must be inside the study area shown on the map")

    def config(self):
        return {
            "schema_version": 1,
            "study_bbox_wgs84": self.bbox,
            "initial_request": self.initial_request,
            "time_budget": time_config(),
            "external_services": self.external.config(),
            "locations": [{"id": row["place_id"], "name": row["name"],
                           "category": row["primary_type"],
                           "lonlat": [row["longitude"], row["latitude"]]}
                          for row in self._places[:200]],
            "normalization": self.engine.normalization,
            "evidence": self.engine.evidence.metadata(),
            "profile": {
                "version": "independent-analysis-v2",
                "schema_version": 2,
                "analysis_systems": ["saju", "astrology"],
                "fusion_policy": "equal-experience-weights-v1",
                "calendar": "gregorian",
                "target_timezone": "America/New_York",
                "default_target_date": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                "birth_date_max": datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat(),
                "persists_birth_data": False,
                "externally_validated": False,
            },
            "capabilities": {
                "weights_affecting_route": ["open", "green", "still"],
                "weights_affecting_venues": ["social", "access"],
                "profile_scale": {"min": 1, "max": 10, "zero_weight_at": 1},
                "venue_radius_m": 120, "venue_limit": 10,
                "venue_ranking_basis": "nearest_road_social_access_model_fit",
                "explicit_via": True, "same_via_for_both_routes": True,
                "live_opening_hours": False, "verified_venue_entrances": False,
                "live_navigation": False, "empirical_quality_verified": False,
                "walking_time_estimate": True,
                "time_budget_constraint": True,
                "estimated_time_includes_snap_connectors": True,
                "arrival_guaranteed": False,
                "distance_includes_snap_connectors": False,
                "venues_auto_visited": False,
                "route_may_leave_study_bbox": True,
                "astrology_interpretation_connected": True,
                "external_weather": True,
                "external_reference_is_recommendation_eta": False,
                "local_route_requires_external_api": False,
            },
        }

    def profile(self, request):
        from .independent_profile import build_independent_profile
        return build_independent_profile(request)

    def external_settings(self, request):
        return self.external.set_ors_key(request)

    def search(self, request):
        """Use ORS when connected; transparently fall back to saved local names."""
        if (not isinstance(request, dict) or set(request) != {"query"}
                or not isinstance(request["query"], str)):
            raise ValueError("Search requires only a query string")
        query = request["query"].strip()
        if not 2 <= len(query) <= 160 or any(ord(c) < 32 for c in query):
            raise ValueError("Search query must contain 2 to 160 visible characters")
        remote = self.external.search({"query": query})
        if remote.get("status") == "ok":
            return remote
        tokens = [_normalized_name(word) for word in query.split()]
        tokens = [word for word in tokens if word]
        rows = []
        for place in self._places:
            name = _normalized_name(place["name"])
            if tokens and all(word in name for word in tokens):
                rows.append({"id": place["place_id"], "name": place["name"],
                             "lonlat": [place["longitude"], place["latitude"]]})
            if len(rows) >= 6:
                break
        explanation = remote.get("message")
        if not isinstance(explanation, str):
            explanation = "외부 주소 검색을 사용할 수 없어요."
        return {"status": "ok", "provider": "local", "results": rows,
                "fallback": True, "reason": remote.get("reason", "unavailable"),
                "message": explanation + " 저장된 동네 장소 이름으로 검색했어요.",
                "attribution": "OpenStreetMap contributors / NYC Open Data · 저장된 장소 자료"}

    def weather(self, request):
        return self.external.weather(request)

    def reference(self, request):
        self._validate_endpoints(request)
        return self.external.reference(request)

    def context(self):
        if self._context is None:
            context_path = self.data_path / "map-context.geojson"
            if not context_path.is_file():
                raise ValueError("Map context has not been prepared for this dataset")
            value = json.loads(context_path.read_text(encoding="utf-8"))
            if value.get("type") != "FeatureCollection" or not isinstance(value.get("features"), list):
                raise ValueError("Map context must be a GeoJSON FeatureCollection")
            json.dumps(value, allow_nan=False)
            self._context = value
        return self._context

    def asset(self, name):
        """Only shipped, explicitly allowlisted assets can leave the package."""
        if name not in ASSET_TYPES:
            return None
        path = Path(__file__).resolve().parent / "web" / name
        if not path.is_file() or path.is_symlink():
            return None
        web_root = (Path(__file__).resolve().parent / "web").resolve()
        if not path.resolve().is_relative_to(web_root):
            return None
        return path.read_bytes(), ASSET_TYPES[name]

    def recommend(self, request):
        self._validate_endpoints(request)
        result = self.engine.recommend(request)
        shortest = self._route_summary(result["shortest"])
        personalized = self._route_summary(result["personalized"]) if result["personalized"] else None
        extra = result["extra_distance_m"]
        result["comparison"] = {
            "shortest": shortest, "personalized": personalized,
            "extra_distance_m": extra,
            "extra_distance_percent": (100 * extra / shortest["length_m"]
                                       if extra is not None and shortest["length_m"] > 1e-8 else None),
            "same_explicit_via": result["explicit_via"],
            "measurement_notes": {
                "open": "Normalized 8-ray horizontal building-clearance model; not measured sky view.",
                "green": "Normalized 2015 Alive street-tree record count within 50 m of source-edge midpoints; not current canopy.",
                "still": "Normalized distance from source-edge midpoints to major roads; not measured noise.",
                "trees_within_radius": "Length-weighted midpoint record count, not unique trees along the route.",
                "major_road_distance_m": "Length-weighted midpoint distance to mapped major roads, in metres.",
            },
        }
        weights, targets = Weights.from_dict(result["weights"]), Targets.from_dict(result["targets"])
        result["venue_candidates"] = (self._venue_candidates(result["personalized"], weights, targets)
                                      if result["personalized"] else [])
        if "budget" in result:
            for venue in result["venue_candidates"]:
                venue["time_budget_checked"] = False
        result["venue_ranking"] = {
            "basis": "nearest_road_social_access_model_fit",
            "order": (["potential_fit_desc", "distance_to_route_m", "place_id"]
                      if weights.social + weights.access else ["distance_to_route_m", "place_id"]),
            "radius_m": 120, "limit": 10,
            "zero_weights_mean": "No fit is assigned; candidates are ordered by distance only.",
            "limitations": ["Nearby road context is not a venue review score.",
                            "Straight-line distance does not prove entrance access or an unobstructed walk.",
                            "Opening status and present experience quality are unverified."],
        }
        json.dumps(result, allow_nan=False)
        return result

    def _eligible_places(self):
        candidates = []
        for source in self.engine.dataset.places:
            category = source.get("primary_type")
            if (category not in VISIT_CATEGORIES or not isinstance(source.get("name"), str)
                    or not source["name"].strip()):
                continue
            try:
                coordinate = _coordinate([source["longitude"], source["latitude"]], "venue")
            except (ValueError, KeyError):
                continue
            if not self._inside(coordinate):
                continue
            if str(source.get("include_in_scoring", "True")).lower() == "false":
                continue
            if str(source.get("access_tag", "")).lower() in {"no", "private"}:
                continue
            row = dict(source)
            row["longitude"], row["latitude"] = coordinate
            row["name"] = str(row["name"]).strip()
            row["place_id"] = str(row.get("place_id") or row.get("source_id") or "")
            if not row["place_id"]:
                continue
            row["_point"] = Point(TO_METRES.transform(*coordinate))
            candidates.append(row)
        # Prefer corroborated identities, then a stable source ID. A repeated
        # name nearby is consolidated; distant branches remain separate.
        candidates.sort(key=lambda p: (p.get("evidence_status") != "crosschecked", p["place_id"]))
        unique, seen_ids, seen_names = [], set(), {}
        for row in candidates:
            name_key = (_normalized_name(row["name"]), row["primary_type"])
            if row["place_id"] in seen_ids or any(row["_point"].distance(other) <= 30
                                                  for other in seen_names.get(name_key, [])):
                continue
            seen_ids.add(row["place_id"])
            seen_names.setdefault(name_key, []).append(row["_point"])
            indices, distances = self._road_tree.query_nearest(row["_point"], all_matches=True, return_distance=True)
            if not len(indices):
                continue
            index, distance = min(zip(indices, distances),
                                  key=lambda pair: (float(pair[1]), self.engine.dataset.edges[int(pair[0])].id))
            row["_edge_id"] = self.engine.dataset.edges[int(index)].id
            row["_road_distance"] = float(distance)
            unique.append(row)
        # Offer varied endpoint choices before restaurant-heavy alphabetical lists.
        priority = {"park": 0, "plaza": 1, "museum": 2, "art_gallery": 3, "cafe": 4}
        return sorted(unique, key=lambda row: (priority.get(row["primary_type"], 5), row["name"].casefold(), row["place_id"]))

    def _venue_candidates(self, route, weights, targets):
        line = transform(TO_METRES.transform, shape(route["route"]["geometry"]))
        choices = []
        for row in self._places:
            distance = float(line.distance(row["_point"]))
            if distance > 120:
                continue
            edge_id = row["_edge_id"]
            fit = evaluate_edge(self.engine.features["edges"][edge_id],
                                self.engine.edge_by_id[edge_id].length, weights, targets)["potential_fit"]
            properties = {
                "place_id": row["place_id"], "name": row["name"], "category": row["primary_type"],
                "lonlat": [row["longitude"], row["latitude"]], "distance_to_route_m": distance,
                "nearest_road_distance_m": row["_road_distance"], "nearest_road_id": edge_id,
                "potential_fit": fit, "fit_basis": "nearest_road_social_access_model_fit",
                "is_venue": True, "route_access_verified": False, "opening_status": "unverified",
                "source": row.get("source", ""), "source_url": row.get("source_url", ""),
                "evidence": {key: row.get(key) or None for key in
                             ("evidence_status", "source_date", "verification_scope", "verification_source")},
            }
            choices.append(properties)
        choices.sort(key=lambda row: (-(row["potential_fit"] or 0), row["distance_to_route_m"], row["place_id"]))
        result = choices[:10]
        for rank, row in enumerate(result, 1):
            row["rank"] = rank
        return result

    def _route_summary(self, route):
        """Weight by actually traversed line pieces, including via backtracking.

        Public route coordinates retain every source-edge vertex. Each piece is
        attributed to a used source edge only when all three sampled points lie
        on it to 0.1 mm. Coincident edges with differing values are left unknown.
        """
        edge_ids = list(dict.fromkeys(route["edge_ids"]))
        roads = [LineString(self.engine.edge_by_id[key].coords) for key in edge_ids]
        index = STRtree(roads)
        coords = [TO_METRES.transform(*point) for point in route["route"]["geometry"]["coordinates"]]
        totals = dict.fromkeys(MODEL_METRICS + RAW_METRICS, 0.0)
        covered = 0.0
        traversed = 0.0
        for start, end in zip(coords, coords[1:]):
            length = math.dist(start, end)
            if length <= 1e-8:
                continue
            traversed += length
            points = [Point(start), Point(end), Point((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)]
            candidates = [int(i) for i in index.query(points[2], predicate="dwithin", distance=1e-4)
                          if all(roads[int(i)].distance(point) <= 1e-4 for point in points)]
            observations = [self.engine.features["edges"][edge_ids[i]] for i in candidates]
            if not observations:
                continue
            values = observations[0]
            if any(any(not math.isclose(float(other.get(key, math.nan)), float(values.get(key, math.nan)), abs_tol=1e-9)
                       for key in totals) for other in observations[1:]):
                continue
            if not all(isinstance(values.get(key), (int, float)) and math.isfinite(values[key]) for key in totals):
                continue
            covered += length
            for key in totals:
                totals[key] += values[key] * length
        coverage = covered / traversed if traversed > 1e-8 else 0.0
        # Do not silently turn an incompletely attributable path into a claimed
        # full-route average. Coverage is exposed for diagnosis instead.
        means = {key: value / covered if covered > 1e-8 and coverage >= 1 - 1e-6 else None
                 for key, value in totals.items()}
        return {
            "length_m": route["length_m"],
            "model_indicators": {key: means[key] for key in MODEL_METRICS},
            "raw_observations": {key: means[key] for key in RAW_METRICS},
            "aggregation": "traversed_length_weighted_source_edge_midpoint_observations",
            "attributed_length_m": covered, "attributed_fraction": coverage,
            "experience_quality_verified": False,
        }
