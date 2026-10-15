"""Exercise the shipped Williamsburg dataset and the real local HTTP API.

Uses prepared JSON only: no OSM downloads, feature regeneration, or external
services. A single engine/server is shared and the listener is closed afterward.
"""

import copy
import http.client
import json
import math
from pathlib import Path
import threading
import unittest

from pyproj import Transformer
from shapely.geometry import shape

from luckypath.engine import Engine
from luckypath.routing import RoutingError
from luckypath.server import make_server


_PROJECT = Path(__file__).resolve().parents[1]
_TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(_PROJECT / "data")
        cls.request = json.loads((_PROJECT / "examples" / "request.json").read_text())
        cls.reference = cls.engine.recommend(cls.request)
        cls.allowed_origin = "http://localhost:5173"
        cls.server = make_server(cls.engine, port=0, allowed_origin=cls.allowed_origin)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        if cls.thread.is_alive():
            raise RuntimeError("Local test HTTP server did not stop.")

    def http(self, method, path, payload=None, headers=None, body=None):
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def assert_valid_route(self, route):
        feature = route["route"]
        self.assertEqual(feature["type"], "Feature")
        self.assertEqual(feature["geometry"]["type"], "LineString")
        geometry = shape(feature["geometry"])
        self.assertTrue(geometry.is_valid)
        self.assertGreater(geometry.length, 0)
        self.assertGreater(route["length_m"], 0)
        self.assertGreater(route["total_cost"], 0)
        coords = feature["geometry"]["coordinates"]
        self.assertEqual(coords[0], route["snapped_origin"])
        self.assertEqual(coords[-1], route["snapped_destination"])
        for lon, lat in coords:
            self.assertTrue(-180 <= lon <= 180 and -90 <= lat <= 90)
        for label in ("origin", "destination"):
            snap = route["snaps"][label]
            self.assertEqual(snap["input_lonlat"], self.request[label])
            projected_input = _TO_METRES.transform(*snap["input_lonlat"])
            projected_snap = _TO_METRES.transform(*snap["lonlat"])
            self.assertAlmostEqual(math.dist(projected_input, projected_snap), snap["distance_m"], places=5)
            self.assertLessEqual(snap["distance_m"], 150)
        self.assertTrue(set(route["edge_ids"]) <= set(self.engine.edge_by_id))

    def test_shipped_example_returns_real_snapped_routes_and_a_shortest_baseline(self):
        result = self.reference
        self.assert_valid_route(result["personalized"])
        self.assert_valid_route(result["shortest"])
        self.assertLessEqual(result["shortest"]["length_m"], result["personalized"]["length_m"] + 1e-6)
        self.assertAlmostEqual(result["extra_distance_m"],
                               result["personalized"]["length_m"] - result["shortest"]["length_m"])
        self.assertEqual(result["geojson"]["type"], "FeatureCollection")
        self.assertEqual(len(result["geojson"]["features"]), 2 + len(result["lucky_stops"]))
        self.assertFalse(result["lucky_stop_auto_added"])
        json.dumps(result, allow_nan=False)

    def test_zero_spatial_weights_produce_the_exact_shortest_route(self):
        request = copy.deepcopy(self.request)
        request["weights"].update(open=0, green=0, still=0)
        result = self.engine.recommend(request)
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         result["shortest"]["route"]["geometry"])
        self.assertEqual(result["personalized"]["edge_ids"], result["shortest"]["edge_ids"])
        self.assertEqual(result["personalized"]["length_m"], result["shortest"]["length_m"])
        self.assertEqual(result["personalized"]["total_cost"], result["shortest"]["total_cost"])
        self.assertEqual(result["extra_distance_m"], 0)

    def test_social_access_change_leaves_route_unchanged_and_rescores_stops(self):
        request = copy.deepcopy(self.request)
        request["weights"].update(social=0, access=1)
        result = self.engine.recommend(request)
        for key in ("length_m", "total_cost", "edge_ids"):
            self.assertEqual(result["personalized"][key], self.reference["personalized"][key])
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         self.reference["personalized"]["route"]["geometry"])
        self.assertTrue(result["lucky_stops"])
        for candidate in result["lucky_stops"]:
            feature = self.engine.features["edges"][candidate["edge_id"]]
            self.assertAlmostEqual(candidate["potential_fit"], 1 - abs(feature["access"] - 0.8))
            self.assertFalse(candidate["route_access_verified"])
        # This pinned example has different candidate lists when access alone
        # determines the stop ranking, while the route remains exactly the same.
        self.assertNotEqual([s["edge_id"] for s in result["lucky_stops"]],
                            [s["edge_id"] for s in self.reference["lucky_stops"]])

    def test_explicit_via_is_present_in_both_routes(self):
        coords = self.reference["personalized"]["route"]["geometry"]["coordinates"]
        # A known reachable point avoids assuming that a suggested stop's
        # component is connected; candidate access is deliberately unverified.
        via = coords[len(coords) // 2]
        result = self.engine.recommend({**self.request, "via": via, "lucky_limit": 0})
        self.assertEqual(result["explicit_via"], via)
        self.assertFalse(result["lucky_stop_auto_added"])
        for label in ("personalized", "shortest"):
            route = result[label]
            snapped = route["snapped_via"]
            self.assertLess(route["snaps"]["via"]["distance_m"], 0.001)
            self.assertTrue(any(math.dist(point, snapped) < 1e-10
                                for point in route["route"]["geometry"]["coordinates"]))

    def test_mixed_input_scales_and_faraway_points_are_rejected(self):
        request = {**self.request, "profile_scores": dict.fromkeys(self.request["weights"], 5)}
        with self.assertRaisesRegex(ValueError, "weights OR profile_scores"):
            self.engine.recommend(request)
        with self.assertRaisesRegex(RoutingError, "maximum snap distance"):
            self.engine.recommend({**self.request, "origin": [-74.5, 41.0]})

    def test_metadata_counts_match_the_loaded_sources_and_feature_contract(self):
        metadata = self.engine.metadata()
        dataset = self.engine.dataset
        self.assertEqual(metadata["osm"]["file"], "will.xml")
        self.assertEqual(metadata["osm"]["source_nodes"], 23895)
        self.assertEqual(metadata["osm"]["source_ways"], 5278)
        self.assertEqual(metadata["counts"]["nodes"], len(dataset.nodes))
        self.assertEqual(metadata["counts"]["edges"], len(dataset.edges))
        self.assertEqual(metadata["counts"]["trees"], len(dataset.trees))
        self.assertEqual(metadata["counts"]["places"], len(dataset.places))
        self.assertEqual(metadata["counts"]["buildings"], len(dataset.buildings))
        self.assertEqual(metadata["counts"]["waterfront_entrances"], len(dataset.entrances))
        self.assertEqual(metadata["feature_parameters"]["tree_radius_m"], 50)
        self.assertEqual(metadata["feature_parameters"]["open_radius_m"], 200)
        self.assertEqual(metadata["weights_affecting_route"], ["open", "green", "still"])
        self.assertEqual(metadata["weights_affecting_stops"], ["social", "access"])

    def test_api_health_and_cors_are_limited_to_configured_origin(self):
        status, headers, payload = self.http("GET", "/health", headers={"Origin": self.allowed_origin})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.allowed_origin)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        status, headers, _ = self.http("OPTIONS", "/route", headers={"Origin": "https://other.example"})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = self.http("OPTIONS", "/route", headers={"Origin": self.allowed_origin})
        self.assertEqual(status, 200)
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_api_valid_route_returns_geojson_matching_direct_engine(self):
        status, headers, result = self.http("POST", "/route", payload={**self.request, "lucky_limit": 0},
                                            headers={"Origin": self.allowed_origin})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.allowed_origin)
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         self.reference["personalized"]["route"]["geometry"])
        self.assertEqual(result["geojson"]["type"], "FeatureCollection")

    def test_api_invalid_route_and_json_return_clear_client_errors(self):
        for body in (b'{"origin":[-73.96,40.715]}', b'{"origin":'):
            with self.subTest(body=body):
                status, _, payload = self.http("POST", "/route", body=body,
                                                headers={"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
                self.assertNotIn("Traceback", payload["error"])
                self.assertNotIn("/Users/", payload["error"])

    def test_api_rejects_oversized_declared_body_before_reading_it(self):
        # Send only two bytes. A server that tries to read all 65537 before
        # checking the declared size would time out instead of returning 413.
        status, _, payload = self.http("POST", "/route", body=b"{}", headers={
            "Content-Type": "application/json", "Content-Length": "65537",
        })
        self.assertEqual(status, 413)
        self.assertIn("65536", payload["error"])


if __name__ == "__main__":
    unittest.main()
