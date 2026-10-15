"""Budgeted local API requests against the prepared NYC walking network.

Uses only shipped data and a temporary loopback HTTP listener. These are
synthetic requests, not field trials or evidence that an arrival is guaranteed.
"""
import copy
import http.client
import json
import math
from pathlib import Path
import threading
import unittest

from luckypath.engine import Engine, TO_LONLAT, TO_METRES
from luckypath.explore import ExploreService
from luckypath.server import make_server


PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT.parent / "luckypath-nyc-data" / "quality" / "pilot" / "williamsburg"


def independent_minutes(route, *, speed=4.5, buffer=3, stop=0):
    """Literal expected formula, without calling the production time helper."""
    snaps = route["snaps"]
    labels = ["origin"] + (["via"] if "via" in snaps else []) + ["destination"]
    connectors = math.fsum(
        snaps[label]["distance_m"]
        for before, after in zip(labels, labels[1:])
        if snaps[before]["input_lonlat"] != snaps[after]["input_lonlat"]
        for label in (before, after)
    )
    return math.fsum((route["length_m"] / (speed * 1000 / 60),
                      connectors / (speed * 1000 / 60), buffer, stop)), connectors


@unittest.skipUnless((DATA / "dataset.json").is_file(), "Requires prepared NYC Williamsburg data")
class BudgetHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(DATA)
        cls.explore = ExploreService(cls.engine, DATA)
        cls.request = copy.deepcopy(cls.explore.initial_request)
        cls.legacy = cls.explore.recommend(cls.request)
        cls.minimum, _ = independent_minutes(cls.legacy["shortest"])
        cls.unconstrained, _ = independent_minutes(cls.legacy["personalized"])
        cls.allowed_origin = "http://localhost:5173"
        cls.server = make_server(cls.engine, port=0, explore=cls.explore,
                                 allowed_origin=cls.allowed_origin)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": .02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        if cls.thread.is_alive():
            raise RuntimeError("Budget test loopback HTTP server did not stop")

    def http(self, path, payload=None, *, headers=None):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST" if payload is not None else "GET", path,
                               body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def budget_request(self, maximum=30, **budget_changes):
        return {**self.request, "time_budget": {"max_minutes": maximum, **budget_changes}}

    def assert_time(self, route, *, speed=4.5, buffer=3, stop=0):
        expected, connectors = independent_minutes(route, speed=speed, buffer=buffer, stop=stop)
        actual = route["time_estimate"]
        self.assertAlmostEqual(actual["total_minutes"], expected, places=10)
        self.assertAlmostEqual(actual["network_distance_m"], route["length_m"], places=10)
        self.assertAlmostEqual(actual["connector_distance_m"], connectors, places=10)
        self.assertAlmostEqual(actual["walking_minutes"], route["length_m"] / (speed * 1000 / 60))
        self.assertAlmostEqual(actual["connector_minutes"], connectors / (speed * 1000 / 60))
        self.assertEqual(actual["walking_speed_kmh"], speed)
        self.assertEqual(actual["stop_minutes"], stop)
        self.assertEqual(actual["buffer_minutes"], buffer)
        self.assertTrue(actual["estimated"])
        self.assertFalse(actual["connectors_access_verified"])
        self.assertFalse(actual["live_delays_included"])
        self.assertFalse(actual["empirically_calibrated"])
        self.assertEqual(route["route"]["properties"]["time_estimate"], actual)

    def test_absent_budget_keeps_legacy_response_and_initial_request_unchanged(self):
        before = copy.deepcopy(self.explore.initial_request)
        status, _, result = self.http("/explore/route", self.request)
        self.assertEqual(status, 200)
        self.assertEqual(result, self.legacy)
        self.assertEqual(result["schema_version"], 1)
        self.assertNotIn("budget", result)
        self.assertNotIn("time_estimate", result["personalized"])
        self.assertNotIn("time_estimate", result["shortest"])
        self.assertEqual(self.explore.initial_request, before)
        # The opt-in field does not enable field-feedback collection.
        self.assertEqual(self.http("/pilot/manifest")[0], 404)

    def test_generous_budget_preserves_original_route_and_exposes_explicit_estimates(self):
        status, headers, result = self.http("/explore/route", self.budget_request(),
                                            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["budget"]["status"], "feasible")
        self.assertFalse(result["budget"]["arrival_guaranteed"])
        self.assertEqual(result["weights"], self.legacy["weights"])
        self.assertEqual(result["personalized"]["route"]["geometry"],
                         self.legacy["personalized"]["route"]["geometry"])
        self.assertAlmostEqual(result["budget"]["minimum_required_minutes"], self.minimum)
        for key in ("shortest", "personalized"):
            self.assert_time(result[key])
            self.assertTrue(result[key]["route"]["properties"]["within_time_budget"])
        self.assertAlmostEqual(result["budget"]["remaining_minutes"],
                               30 - result["personalized"]["time_estimate"]["total_minutes"])
        self.assertEqual(result["geojson"]["budget"], result["budget"])
        self.assertTrue(all(not venue["time_budget_checked"] for venue in result["venue_candidates"]))
        json.dumps(result, allow_nan=False)

    def test_shortest_feasible_budget_selects_an_alternative_to_an_overlong_original(self):
        self.assertGreater(self.unconstrained, self.minimum + .001,
                           "Pinned NYC fixture must have a genuine personalized detour")
        maximum = (self.minimum + self.unconstrained) / 2
        status, _, result = self.http("/explore/route", self.budget_request(maximum))
        self.assertEqual(status, 200)
        self.assertEqual(result["budget"]["status"], "feasible")
        self.assertIsNotNone(result["personalized"])
        self.assertLessEqual(result["personalized"]["time_estimate"]["total_minutes"], maximum)
        self.assertNotEqual(result["personalized"]["route"]["geometry"],
                            self.legacy["personalized"]["route"]["geometry"])
        self.assertEqual(result["shortest"]["route"]["geometry"],
                         self.legacy["shortest"]["route"]["geometry"])
        self.assertGreaterEqual(result["personalized"]["total_cost"] + 1e-7,
                                self.legacy["personalized"]["total_cost"])
        selection = result["budget"]["selection"]
        self.assertTrue(selection["original_cost_preserved"])
        self.assertFalse(selection["global_constrained_optimum_guaranteed"])
        self.assertLessEqual(selection["search_count"], selection["search_limit"])
        self.assertGreaterEqual(selection["feasible_candidate_count"], 1)
        self.assert_time(result["personalized"])

    def test_infeasible_budget_returns_200_with_minimum_and_no_recommended_path(self):
        for path in ("/route", "/explore/route"):
            with self.subTest(path=path):
                status, _, result = self.http(path, self.budget_request(1))
                self.assertEqual(status, 200)
                self.assertEqual(result["budget"]["status"], "infeasible")
                self.assertEqual(result["budget"]["reason"], "minimum_estimated_time_exceeds_budget")
                self.assertAlmostEqual(result["budget"]["minimum_required_minutes"], self.minimum)
                self.assertIsNone(result["personalized"])
                self.assertIsNone(result["extra_distance_m"])
                self.assertIsNone(result["budget"]["estimated_total_minutes"])
                self.assertIsNone(result["budget"]["remaining_minutes"])
                self.assertIsNone(result["budget"]["selection"])
                self.assertEqual(result["lucky_stops"], [])
                self.assertEqual(result["geojson"]["features"], [result["shortest"]["route"]])
                self.assertFalse(result["shortest"]["route"]["properties"]["within_time_budget"])
                self.assert_time(result["shortest"])
                if path.startswith("/explore"):
                    self.assertIsNone(result["comparison"]["personalized"])
                    self.assertIsNone(result["comparison"]["extra_distance_percent"])
                    self.assertEqual(result["venue_candidates"], [])

    def test_minimum_boundary_uses_full_precision_instead_of_display_rounding(self):
        just_below = self.minimum - 1e-7
        self.assertEqual(round(just_below, 2), round(self.minimum, 2))
        status, _, rejected = self.http("/route", self.budget_request(just_below))
        self.assertEqual(status, 200)
        self.assertEqual(rejected["budget"]["status"], "infeasible")
        status, _, accepted = self.http("/route", self.budget_request(self.minimum))
        self.assertEqual(status, 200)
        self.assertEqual(accepted["budget"]["status"], "feasible")
        self.assertAlmostEqual(accepted["personalized"]["time_estimate"]["total_minutes"], self.minimum, places=9)

    def test_explicit_via_reserves_return_connector_and_stop_in_both_routes(self):
        coords = self.legacy["personalized"]["route"]["geometry"]["coordinates"]
        x, y = TO_METRES.transform(*coords[len(coords) // 2])
        via = list(TO_LONLAT.transform(x + 4, y + 7))
        request = {**self.budget_request(60, stop_minutes=8, walking_speed_kmh=3.5,
                                       buffer_minutes=5), "via": via}
        status, _, result = self.http("/explore/route", request)
        self.assertEqual(status, 200)
        self.assertEqual(result["budget"]["status"], "feasible")
        self.assertEqual(result["explicit_via"], via)
        self.assertEqual(result["comparison"]["same_explicit_via"], via)
        for key in ("shortest", "personalized"):
            route = result[key]
            self.assert_time(route, speed=3.5, buffer=5, stop=8)
            self.assertGreater(route["snaps"]["via"]["distance_m"], .01)
            expected = (route["snaps"]["origin"]["distance_m"]
                        + route["snaps"]["destination"]["distance_m"]
                        + 2 * route["snaps"]["via"]["distance_m"])
            self.assertAlmostEqual(route["time_estimate"]["connector_distance_m"], expected)
            self.assertIn(route["snapped_via"], route["route"]["geometry"]["coordinates"])
        self.assertAlmostEqual(result["budget"]["minimum_required_minutes"],
                               result["shortest"]["time_estimate"]["total_minutes"])

    def test_same_endpoint_requires_only_configured_fixed_time_and_no_fabricated_loop(self):
        request = {**self.budget_request(3), "destination": self.request["origin"]}
        status, _, result = self.http("/route", request)
        self.assertEqual(status, 200)
        self.assertEqual(result["budget"]["status"], "feasible")
        for key in ("shortest", "personalized"):
            route = result[key]
            self.assertEqual(route["length_m"], 0)
            self.assertEqual(route["edge_ids"], [])
            self.assertEqual(route["time_estimate"]["connector_distance_m"], 0)
            self.assertEqual(route["time_estimate"]["total_minutes"], 3)
        status, _, too_short = self.http("/route", {**request, "time_budget": {"max_minutes": 2.99}})
        self.assertEqual(status, 200)
        self.assertEqual(too_short["budget"]["status"], "infeasible")
        self.assertEqual(too_short["budget"]["minimum_required_minutes"], 3)

    def test_invalid_budget_values_return_client_errors_before_any_route(self):
        bad = [None, [], {}, {"max_minutes": 30, "extra": 1},
               {"max_minutes": 30, "stop_minutes": 1}]
        for key in ("max_minutes", "walking_speed_kmh", "buffer_minutes", "stop_minutes"):
            for value in (True, "3", float("nan"), float("inf"), 10 ** 1000):
                bad.append({"max_minutes": 30, key: value})
        bad.extend({"max_minutes": value} for value in (0, 240.01))
        for index, value in enumerate(bad):
            with self.subTest(index=index):
                status, _, result = self.http("/route", {**self.request, "time_budget": value})
                self.assertEqual(status, 400)
                self.assertIn("time_budget", result["error"])
                self.assertNotIn("Traceback", result["error"])
                self.assertNotIn("/Users/", result["error"])

    def test_time_request_does_not_accept_or_echo_birth_data_or_write_prepared_files(self):
        watched = [path for path in DATA.rglob("*") if path.is_file()]
        before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in watched}
        marker = "synthetic-private-birth-value-not-to-echo"
        status, _, result = self.http("/route", {**self.budget_request(), "birth_date": marker})
        self.assertEqual(status, 400)
        self.assertNotIn(marker, json.dumps(result))
        status, _, result = self.http("/route", self.budget_request(),
                                      headers={"Origin": self.allowed_origin})
        self.assertEqual(status, 200)
        for key in ("birth_date", "birth_time", "birth_timezone"):
            self.assertNotIn(key, json.dumps(result))
        after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                 for path in DATA.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_config_cors_and_local_explore_origin_rules_still_apply(self):
        status, _, config = self.http("/explore/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["capabilities"]["time_budget_constraint"])
        self.assertTrue(config["capabilities"]["walking_time_estimate"])
        self.assertFalse(config["capabilities"]["arrival_guaranteed"])
        self.assertFalse(config["time_budget"]["external_api_required"])
        status, headers, _ = self.http("/route", self.budget_request(1),
                                        headers={"Origin": self.allowed_origin})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.allowed_origin)
        status, headers, _ = self.http("/route", self.budget_request(1),
                                        headers={"Origin": "https://other.example"})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _ = self.http("/explore/route", self.budget_request(),
                                 headers={"Origin": "https://other.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
