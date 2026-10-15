"""Exercise actual NYC v2 routes and the bounded, local map adapter.

Run with the sibling luckypath-nyc-data collection present. No source download,
field feedback, geolocation or external tile request is made by these tests.
"""
import copy
import http.client
import json
import math
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from luckypath.data import Edge
from luckypath.engine import Engine, TO_LONLAT, TO_METRES
from luckypath.explore import ExploreService, VISIT_CATEGORIES
from luckypath.scoring import Targets, Weights, evaluate_edge
from luckypath.server import make_server


PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT.parent / "luckypath-nyc-data" / "quality" / "pilot" / "williamsburg"


class ComparisonTests(unittest.TestCase):
    def service_and_route(self):
        # 25 -> 150 -> 25 m passes partially over both edges and backtracks.
        # Length weights are 150/250 for a and 100/250 for b, not the full
        # source-edge ratio 1/2 and not a geometry-union measurement.
        x, y = TO_METRES.transform(-73.96, 40.717)
        edges = [Edge("a", "0", "1", [(x, y), (x + 100, y)], 100, {}),
                 Edge("b", "1", "2", [(x + 100, y), (x + 200, y)], 100, {})]
        features = {
            "a": {"open": .2, "green": .2, "still": .2, "trees_within_radius": 2, "major_road_distance_m": 20},
            "b": {"open": .8, "green": .8, "still": .8, "trees_within_radius": 10, "major_road_distance_m": 80},
        }
        service = object.__new__(ExploreService)
        service.engine = SimpleNamespace(edge_by_id={edge.id: edge for edge in edges}, features={"edges": features})
        coords = [list(TO_LONLAT.transform(x + offset, y)) for offset in (25, 100, 150, 100, 25)]
        route = {"edge_ids": ["a", "b", "a"], "length_m": 250,
                 "route": {"geometry": {"type": "LineString", "coordinates": coords}}}
        return service, route

    def test_metrics_weight_actual_partial_segments_and_via_backtracking(self):
        service, route = self.service_and_route()
        result = service._route_summary(route)
        self.assertAlmostEqual(result["attributed_length_m"], 250, places=5)
        self.assertEqual(result["attributed_fraction"], 1)
        self.assertAlmostEqual(result["model_indicators"]["green"], .44, places=8)
        self.assertAlmostEqual(result["raw_observations"]["trees_within_radius"], 5.2, places=8)
        self.assertFalse(result["experience_quality_verified"])

    def test_ambiguous_coincident_edges_do_not_manufacture_a_full_route_average(self):
        service, route = self.service_and_route()
        edge = service.engine.edge_by_id["a"]
        service.engine.edge_by_id["other"] = Edge("other", edge.u, edge.v, edge.coords, edge.length, {})
        service.engine.features["edges"]["other"] = {**service.engine.features["edges"]["a"], "green": .9}
        route["edge_ids"].append("other")
        result = service._route_summary(route)
        self.assertAlmostEqual(result["attributed_fraction"], .4, places=8)
        self.assertIsNone(result["model_indicators"]["green"])

    def test_places_deduplicate_nearby_identity_and_exclude_non_visit_records(self):
        from shapely.geometry import LineString
        service, _ = self.service_and_route()
        edge = service.engine.edge_by_id["a"]
        service.bbox = [-73.97, 40.709, -73.951, 40.724]
        base = {"place_id": "base", "name": "Example Cafe", "primary_type": "cafe",
                "longitude": -73.96, "latitude": 40.717, "evidence_status": "partial"}
        service.engine.dataset = SimpleNamespace(edges=[edge], places=[
            base,
            {**base, "place_id": "official", "name": "EXAMPLE CAFE", "evidence_status": "crosschecked"},
            {**base, "place_id": "other_branch", "longitude": -73.958},
            {**base, "place_id": "clinic", "primary_type": "medical_clinic"},
            {**base, "place_id": "private", "name": "Private", "access_tag": "private"},
            {**base, "place_id": "unscored", "name": "Unscored", "include_in_scoring": "False"},
            {**base, "place_id": "unnamed", "name": None},
            {**base, "place_id": "outside", "longitude": -74},
        ])
        service._road_tree = STRtree([LineString(edge.coords)])
        result = service._eligible_places()
        self.assertEqual({p["place_id"] for p in result}, {"official", "other_branch"})


@unittest.skipUnless((DATA / "dataset.json").is_file(), "Requires sibling NYC v2 Williamsburg prepared data")
class ExploreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(DATA)
        cls.explore = ExploreService(cls.engine, DATA)
        cls.request = cls.explore.initial_request
        cls.reference = cls.explore.recommend(cls.request)
        cls.server = make_server(cls.engine, port=0, explore=cls.explore)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": .05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def http(self, method, path, payload=None, headers=None, body=None):
        headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_config_context_and_original_health_contract(self):
        status, _, body = self.http("GET", "/explore/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertEqual(config["study_bbox_wgs84"], [-73.97, 40.709, -73.951, 40.724])
        self.assertEqual(config["initial_request"], self.request)
        self.assertEqual(config["capabilities"]["weights_affecting_route"], ["open", "green", "still"])
        self.assertFalse(config["capabilities"]["verified_venue_entrances"])
        self.assertGreater(len(config["locations"]), 0)
        for row in config["locations"]:
            self.assertTrue(self.explore._inside(row["lonlat"]))
        status, _, body = self.http("GET", "/explore/context")
        self.assertEqual(status, 200)
        context = json.loads(body)
        self.assertEqual(context["type"], "FeatureCollection")
        self.assertTrue(context["features"])
        for path in ("/", "/health"):
            status, _, body = self.http("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])

    def test_invalid_or_outside_origin_destination_and_via_are_rejected(self):
        invalid = [None, [True, 40.717], ["-73.96", 40.717], [-74, 40.717],
                   [-73.96, 40.8], [float("nan"), 40.717], [-73.96]]
        for label in ("origin", "destination", "via"):
            for value in invalid:
                if label == "via" and value is None:
                    continue
                with self.subTest(label=label, value=value):
                    status, _, body = self.http("POST", "/explore/route", {**self.request, label: value})
                    self.assertEqual(status, 400)
                    self.assertIn(label, json.loads(body)["error"])

    def test_explore_is_opt_in_and_legacy_route_matches_engine(self):
        status, _, body = self.http("POST", "/route", self.request)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertNotIn("venue_candidates", result)
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         self.reference["personalized"]["route"]["geometry"])
        other = make_server(self.engine, port=0)
        try:
            thread = threading.Thread(target=other.handle_request, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", other.server_address[1], timeout=10)
            try:
                connection.request("GET", "/explore/config")
                self.assertEqual(connection.getresponse().status, 404)
            finally:
                connection.close()
            thread.join(timeout=2)
        finally:
            other.server_close()

    def test_real_venues_are_bounded_named_and_ranked_by_explicit_existing_fit(self):
        line = transform(TO_METRES.transform, shape(self.reference["personalized"]["route"]["geometry"]))
        candidates = self.reference["venue_candidates"]
        self.assertTrue(candidates)
        source_ids = {p["place_id"] for p in self.engine.dataset.places}
        keys = []
        for row in candidates:
            self.assertIn(row["place_id"], source_ids)
            self.assertIn(row["category"], VISIT_CATEGORIES)
            self.assertTrue(row["name"])
            self.assertTrue(self.explore._inside(row["lonlat"]))
            distance = line.distance(Point(TO_METRES.transform(*row["lonlat"])))
            self.assertAlmostEqual(row["distance_to_route_m"], distance)
            self.assertLessEqual(distance, 120)
            edge_id = row["nearest_road_id"]
            expected = evaluate_edge(self.engine.features["edges"][edge_id],
                                     self.engine.edge_by_id[edge_id].length,
                                     Weights.from_dict(self.reference["weights"]), Targets())["potential_fit"]
            self.assertAlmostEqual(row["potential_fit"], expected)
            self.assertFalse(row["route_access_verified"])
            self.assertEqual(row["opening_status"], "unverified")
            keys.append((-row["potential_fit"], distance, row["place_id"]))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len({row["place_id"] for row in candidates}), len(candidates))
        json.dumps(self.reference, allow_nan=False)

    def test_zero_social_access_weights_assign_no_fit_and_use_distance(self):
        request = copy.deepcopy(self.request)
        request["profile_scores"].update(social=1, access=1)
        result = self.explore.recommend(request)
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         self.reference["personalized"]["route"]["geometry"])
        keys = [(r["distance_to_route_m"], r["place_id"]) for r in result["venue_candidates"]]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(row["potential_fit"] is None for row in result["venue_candidates"]))

    def test_selected_real_venue_via_constrains_both_comparison_routes(self):
        via = self.reference["venue_candidates"][0]["lonlat"]
        status, _, body = self.http("POST", "/explore/route", {**self.request, "via": via})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["comparison"]["same_explicit_via"], via)
        for kind in ("personalized", "shortest"):
            route = result[kind]
            self.assertEqual(route["snaps"]["via"]["input_lonlat"], via)
            self.assertIn(route["snapped_via"], route["route"]["geometry"]["coordinates"])
            self.assertGreater(route["snaps"]["via"]["distance_m"], 0)
        self.assertFalse(result["lucky_stop_auto_added"])

    def test_zero_length_request_has_null_aggregates_and_finite_json(self):
        result = self.explore.recommend({**self.request, "destination": self.request["origin"]})
        self.assertEqual(result["personalized"]["length_m"], 0)
        self.assertIsNone(result["comparison"]["extra_distance_percent"])
        self.assertTrue(all(value is None for value in result["comparison"]["personalized"]["model_indicators"].values()))
        json.dumps(result, allow_nan=False)

    def test_static_allowlist_blocks_traversal_and_has_exact_mime(self):
        for path in ("../server.py", "%2e%2e/server.py", "vendor/../../server.py", "unknown.js"):
            status, _, _ = self.http("GET", "/explore/assets/" + path)
            self.assertEqual(status, 404)
        if not (PROJECT / "luckypath" / "web" / "index.html").exists():
            self.skipTest("Frontend assets have not been created yet")
        status, headers, body = self.http("GET", "/explore")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"<!", body[:20])
        status, headers, _ = self.http("GET", "/explore/assets/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_cross_origin_host_body_size_and_non_json_requests_are_rejected(self):
        status, _, _ = self.http("POST", "/explore/route", self.request, {"Origin": "https://other.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.http("GET", "/explore/config", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.http("POST", "/explore/route", body=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.http("POST", "/explore/route", body=b"{}", headers={"Content-Type": "application/json", "Content-Length": "65537"})
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
