"""Time assumptions are explicit and computed from travelled network metres.

These small fixtures test the policy independently of path selection or HTTP.
No live traffic, speed service, tile request or actual field observation is used.
"""
import copy
import math
import unittest

from luckypath.travel_time import (
    config, connector_distance, estimate_route_time, max_network_length,
    parse_time_budget,
)


def route_fixture(length=750, *, via=False):
    result = {"length_m": length, "snaps": {
        "origin": {"input_lonlat": [-73.96, 40.71], "distance_m": 30},
        "destination": {"input_lonlat": [-73.95, 40.72], "distance_m": 45},
    }}
    if via:
        result["snaps"]["via"] = {"input_lonlat": [-73.955, 40.715], "distance_m": 15}
    return result


class TravelTimeTests(unittest.TestCase):
    def test_required_maximum_and_defaults_do_not_mutate_request(self):
        request = {"max_minutes": 30}
        before = copy.deepcopy(request)
        parsed = parse_time_budget(request)
        self.assertEqual(parsed, {"max_minutes": 30.0, "walking_speed_kmh": 4.5,
                                  "buffer_minutes": 3.0, "stop_minutes": 0.0})
        self.assertEqual(request, before)
        for invalid in (None, False, 30, [], {}, {"max_minutes": 30, "unknown": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_time_budget(invalid)

    def test_numeric_limits_are_inclusive_and_all_fields_are_strict(self):
        limits = {"max_minutes": (1, 240), "walking_speed_kmh": (2, 6),
                  "buffer_minutes": (0, 30), "stop_minutes": (0, 120)}
        invalid_values = [None, True, False, "3", [], {}, float("nan"),
                          float("inf"), -float("inf"), 10 ** 1000]
        for key, (low, high) in limits.items():
            for valid in (low, high, (low + high) / 2):
                with self.subTest(key=key, valid=valid):
                    parsed = parse_time_budget({"max_minutes": 30, key: valid}, has_via=True)
                    self.assertEqual(parsed[key], valid)
            for invalid in [*invalid_values, low - .01, high + .01]:
                with self.subTest(key=key, invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                    parse_time_budget({"max_minutes": 30, key: invalid}, has_via=True)

    def test_positive_stop_requires_explicit_via_but_zero_does_not(self):
        parse_time_budget({"max_minutes": 30, "stop_minutes": 0})
        with self.assertRaisesRegex(ValueError, "via"):
            parse_time_budget({"max_minutes": 30, "stop_minutes": .01})
        self.assertEqual(parse_time_budget({"max_minutes": 30, "stop_minutes": 5},
                                          has_via=True)["stop_minutes"], 5)

    def test_network_walk_and_unverified_connectors_have_separate_minutes(self):
        route = route_fixture()
        budget = parse_time_budget({"max_minutes": 30})
        original = copy.deepcopy((route, budget))
        result = estimate_route_time(route, budget)
        self.assertEqual(result["network_distance_m"], 750)
        self.assertEqual(result["connector_distance_m"], 75)
        self.assertEqual(result["walking_minutes"], 10)
        self.assertEqual(result["connector_minutes"], 1)
        self.assertEqual(result["buffer_minutes"], 3)
        self.assertEqual(result["total_minutes"], 14)
        self.assertTrue(result["estimated"])
        self.assertFalse(result["connectors_access_verified"])
        self.assertFalse(result["live_delays_included"])
        self.assertFalse(result["empirically_calibrated"])
        self.assertEqual((route, budget), original)

    def test_via_connector_is_walked_twice_and_stop_is_counted_once(self):
        route = route_fixture(via=True)
        budget = parse_time_budget({"max_minutes": 30, "stop_minutes": 5}, has_via=True)
        result = estimate_route_time(route, budget)
        self.assertEqual(connector_distance(route), 30 + 2 * 15 + 45)
        self.assertEqual(result["walking_minutes"], 10)
        self.assertAlmostEqual(result["connector_minutes"], 1.4)
        self.assertEqual(result["stop_minutes"], 5)
        self.assertAlmostEqual(result["total_minutes"], 19.4)

    def test_duplicate_endpoint_via_does_not_add_an_empty_leg(self):
        for endpoint in ("origin", "destination"):
            with self.subTest(endpoint=endpoint):
                route = route_fixture(via=True)
                route["snaps"]["via"] = copy.deepcopy(route["snaps"][endpoint])
                self.assertEqual(connector_distance(route), 75)
        route = route_fixture(0, via=True)
        route["snaps"]["destination"] = copy.deepcopy(route["snaps"]["origin"])
        route["snaps"]["via"] = copy.deepcopy(route["snaps"]["origin"])
        self.assertEqual(connector_distance(route), 0)
        result = estimate_route_time(route, parse_time_budget({"max_minutes": 3}))
        self.assertEqual(result["total_minutes"], 3)
        self.assertEqual(result["walking_minutes"], 0)

    def test_return_trip_with_distinct_via_keeps_both_approaches(self):
        route = route_fixture(1500, via=True)
        route["snaps"]["destination"] = copy.deepcopy(route["snaps"]["origin"])
        self.assertEqual(connector_distance(route), 2 * 30 + 2 * 15)
        result = estimate_route_time(route, parse_time_budget({"max_minutes": 30}))
        self.assertEqual(result["walking_minutes"], 20)
        self.assertAlmostEqual(result["total_minutes"], 24.2)

    def test_remaining_network_allowance_accounts_for_every_time_component(self):
        route = route_fixture(via=True)
        budget = parse_time_budget({"max_minutes": 20, "stop_minutes": 5}, has_via=True)
        self.assertEqual(max_network_length(budget, route), (20 - 5 - 3) * 75 - 105)
        tight = parse_time_budget({"max_minutes": 1, "buffer_minutes": 3})
        self.assertEqual(max_network_length(tight, route), -255)
        # Negative allowance is retained so the caller can mark infeasibility;
        # clamping it to zero could accidentally accept a zero-metre route.
        self.assertLess(max_network_length(tight, route), 0)

    def test_subsecond_overrun_is_not_hidden_by_rounding(self):
        route = route_fixture(750.0000001)
        budget = parse_time_budget({"max_minutes": 14})
        self.assertGreater(estimate_route_time(route, budget)["total_minutes"], 14)
        self.assertEqual(max_network_length(budget, route), 750)
        route["length_m"] = 750
        self.assertEqual(estimate_route_time(route, budget)["total_minutes"], 14)
        slow = parse_time_budget({"max_minutes": 30, "walking_speed_kmh": 3})
        self.assertEqual(estimate_route_time(route, slow)["walking_minutes"], 15)

    def test_config_describes_local_estimate_and_returns_independent_defaults(self):
        first = config()
        self.assertTrue(first["estimated"])
        self.assertFalse(first["arrival_guaranteed"])
        self.assertFalse(first["external_api_required"])
        self.assertFalse(first["empirically_calibrated"])
        self.assertTrue(first["includes_connectors"])
        first["defaults"]["walking_speed_kmh"] = 999
        first["limits"]["max_minutes"]["max"] = 999
        self.assertEqual(config()["defaults"]["walking_speed_kmh"], 4.5)
        self.assertEqual(config()["limits"]["max_minutes"]["max"], 240)
        self.assertTrue(all(math.isfinite(value) for value in config()["defaults"].values()))


if __name__ == "__main__":
    unittest.main()
