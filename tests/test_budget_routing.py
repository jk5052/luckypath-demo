"""Hard network budgets, original costs, and deterministic candidate selection."""

import copy
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyproj import Transformer

from luckypath import routing
from luckypath.routing import BudgetRouteError, RoutingError, plan_budget_route, plan_route


_TO_LONLAT = Transformer.from_crs(32618, 4326, always_xy=True)
_TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)
_X, _Y = _TO_METRES.transform(-73.96, 40.715)


def xy(x, y=0):
    return _X + x, _Y + y


def lonlat(x, y=0):
    return _TO_LONLAT.transform(*xy(x, y))


def edge(edge_id, u, v, coords):
    return SimpleNamespace(
        id=edge_id, u=u, v=v, coords=coords,
        length=sum(math.dist(a, b) for a, b in zip(coords, coords[1:])),
        osm_tags={},
    )


def three_alternatives():
    # All three roads share endpoint IDs; intermediate vertices do not connect.
    dataset = SimpleNamespace(
        nodes={"a": xy(0), "b": xy(100)},
        edges=[
            edge("short", "a", "b", [xy(0), xy(100)]),
            edge("middle", "a", "b", [xy(0), xy(0, 25), xy(100, 25), xy(100)]),
            edge("long", "a", "b", [xy(0), xy(0, 100), xy(100, 100), xy(100)]),
        ],
    )
    return dataset, {"short": 100.0, "middle": 50.0, "long": 10.0}


def single_road():
    return SimpleNamespace(nodes={"a": xy(0), "b": xy(200)},
                           edges=[edge("ab", "a", "b", [xy(0), xy(200)])])


class BudgetRoutingTests(unittest.TestCase):
    def test_intermediate_feasible_route_beats_shortest_by_original_cost(self):
        dataset, costs = three_alternatives()
        unconstrained = plan_route(dataset, lonlat(0), lonlat(100), costs)
        self.assertEqual(unconstrained["edge_ids"], ["long"])
        result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 160)
        self.assertEqual(result["edge_ids"], ["middle"])
        self.assertAlmostEqual(result["length_m"], 150, places=7)
        self.assertEqual(result["total_cost"], 50)
        self.assertEqual(result["route"]["properties"]["total_cost"], 50)
        self.assertLessEqual(result["length_m"], 160 + 1e-8)
        selection = result["budget_selection"]
        self.assertEqual(selection["selected_candidate"], "length_penalty")
        self.assertEqual(selection["min_network_length_m"], 100)
        self.assertEqual(selection["unconstrained_network_length_m"], 300)
        self.assertFalse(selection["global_constrained_optimum_guaranteed"])
        self.assertTrue(selection["original_cost_preserved"])
        self.assertLessEqual(selection["search_count"], selection["search_limit"])
        self.assertLessEqual(selection["search_limit"], 50)
        json.dumps(result, allow_nan=False)

    def test_unconstrained_route_that_fits_preserves_legacy_fields(self):
        dataset, costs = three_alternatives()
        ordinary = plan_route(dataset, lonlat(0), lonlat(100), costs)
        result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 300)
        selection = result.pop("budget_selection")
        self.assertEqual(result, ordinary)
        self.assertEqual(selection["selected_candidate"], "original_personalized")
        self.assertEqual(selection["search_count"], 2)

    def test_tight_budget_uses_shortest_geometry_with_original_weighted_cost(self):
        dataset, costs = three_alternatives()
        costs["short"] = 700
        result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 100)
        self.assertEqual(result["edge_ids"], ["short"])
        self.assertEqual(result["length_m"], 100)
        self.assertEqual(result["total_cost"], 700)
        self.assertEqual(result["route"]["properties"]["total_cost"], 700)

    def test_infeasible_budget_exposes_exact_minimum(self):
        dataset, costs = three_alternatives()
        with self.assertRaises(BudgetRouteError) as caught:
            plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 99)
        self.assertIsInstance(caught.exception, RoutingError)
        self.assertEqual(caught.exception.min_network_length_m, 100)
        self.assertEqual(caught.exception.max_network_length_m, 99)

    def test_budget_boundary_allows_only_documented_numeric_tolerance(self):
        dataset, costs = three_alternatives()
        for cap in (100, 100 - 0.5e-8):
            with self.subTest(cap=cap):
                result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, cap)
                self.assertLessEqual(result["length_m"], cap + 1e-8)
        with self.assertRaises(BudgetRouteError):
            plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 100 - 2e-8)

    def test_via_budget_is_total_across_both_legs(self):
        dataset = single_road()
        # Both legs separately fit 200 m, but their 300 m total cannot fit.
        with self.assertRaises(BudgetRouteError) as caught:
            plan_budget_route(dataset, lonlat(0), lonlat(100), {"ab": 80}, 200,
                              via_lonlat=lonlat(200))
        self.assertAlmostEqual(caught.exception.min_network_length_m, 300, places=7)
        result = plan_budget_route(dataset, lonlat(0), lonlat(100), {"ab": 80}, 300,
                                   via_lonlat=lonlat(200))
        self.assertAlmostEqual(result["length_m"], 300, places=7)
        self.assertAlmostEqual(result["total_cost"], 120, places=7)
        coords = result["route"]["geometry"]["coordinates"]
        actual_x = [_TO_METRES.transform(*coord)[0] - _X for coord in coords]
        self.assertEqual(len(actual_x), 4)
        for actual, expected in zip(actual_x, (0, 100, 200, 100)):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertIn("snapped_via", result)
        self.assertTrue(result["budget_selection"]["via_uses_shared_budget"])

    def test_via_search_allocates_one_budget_without_per_leg_halves(self):
        dataset, costs = three_alternatives()
        # A required final 20 m leg leaves 160 m for the variable first leg.
        dataset.nodes["c"] = xy(120)
        dataset.edges.append(edge("bc", "b", "c", [xy(100), xy(120)]))
        costs["bc"] = 7
        result = plan_budget_route(dataset, lonlat(0), lonlat(120), costs, 180,
                                   via_lonlat=lonlat(100))
        self.assertEqual(result["edge_ids"], ["middle", "bc"])
        self.assertAlmostEqual(result["length_m"], 170, places=6)
        self.assertEqual(result["total_cost"], 57)

    def test_partial_roads_use_actual_geometry_not_declared_length(self):
        dataset = single_road()
        dataset.edges[0].length = 9999
        legacy = plan_route(dataset, lonlat(20, 3), lonlat(80, -4), {"ab": 80})
        result = plan_budget_route(dataset, lonlat(20, 3), lonlat(80, -4), {"ab": 80},
                                   legacy["length_m"])
        self.assertAlmostEqual(result["length_m"], 60, places=6)
        self.assertAlmostEqual(result["total_cost"], 24, places=6)
        self.assertAlmostEqual(result["snaps"]["origin"]["distance_m"], 3, places=6)
        self.assertAlmostEqual(result["snaps"]["destination"]["distance_m"], 4, places=6)
        self.assertFalse(result["budget_selection"]["includes_snap_connectors"])

    def test_identical_endpoints_return_zero_without_inventing_a_loop(self):
        result = plan_budget_route(single_road(), lonlat(40), lonlat(40), {"ab": 80}, 0)
        self.assertEqual(result["length_m"], 0)
        self.assertEqual(result["total_cost"], 0)
        self.assertEqual(result["edge_ids"], [])
        coords = result["route"]["geometry"]["coordinates"]
        self.assertEqual(len(coords), 2)
        self.assertEqual(coords[0], coords[1])

    def test_identical_endpoints_still_obey_requested_via(self):
        with self.assertRaises(BudgetRouteError) as caught:
            plan_budget_route(single_road(), lonlat(0), lonlat(0), {"ab": 80}, 100,
                              via_lonlat=lonlat(100))
        self.assertAlmostEqual(caught.exception.min_network_length_m, 200, places=7)

    def test_zero_and_tiny_costs_are_supported_without_changing_budget(self):
        for scale in (0, 1e-200, 1e200):
            with self.subTest(scale=scale):
                dataset, costs = three_alternatives()
                costs = {key: cost * scale for key, cost in costs.items()}
                result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 160)
                self.assertLessEqual(result["length_m"], 160 + 1e-8)
                self.assertTrue(math.isfinite(result["total_cost"]))
                if scale:
                    self.assertEqual(result["edge_ids"], ["middle"])
                    self.assertAlmostEqual(result["total_cost"] / scale, 50)
                else:
                    self.assertEqual(result["total_cost"], 0)

    def test_invalid_caps_fail_before_search(self):
        dataset, costs = three_alternatives()
        for cap in (-1, float("nan"), float("inf"), -float("inf"), None, True, "bad"):
            with self.subTest(cap=cap), self.assertRaisesRegex(RoutingError, "max_network_length_m"):
                plan_budget_route(dataset, lonlat(0), lonlat(100), costs, cap)

    def test_disconnected_via_is_not_reported_as_a_budget_failure(self):
        dataset = single_road()
        dataset.nodes.update({"c": xy(0, 100), "d": xy(100, 100)})
        dataset.edges.append(edge("cd", "c", "d", [xy(0, 100), xy(100, 100)]))
        with self.assertRaisesRegex(RoutingError, "origin to via") as caught:
            plan_budget_route(dataset, lonlat(10), lonlat(90), {"ab": 1, "cd": 1}, 1000,
                              via_lonlat=lonlat(50, 100))
        self.assertNotIsInstance(caught.exception, BudgetRouteError)

    def test_distant_input_never_substitutes_a_direct_line(self):
        with self.assertRaisesRegex(RoutingError, "maximum snap distance"):
            plan_budget_route(single_road(), lonlat(10, 151), lonlat(90), {"ab": 1}, 1000)

    def test_graph_is_built_once_and_repeated_route_is_stable(self):
        dataset, costs = three_alternatives()
        before, before_costs = copy.deepcopy(dataset), costs.copy()
        with patch.object(routing, "_graph", wraps=routing._graph) as graph_builder:
            result = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 160)
        self.assertEqual(graph_builder.call_count, 1)
        again = plan_budget_route(dataset, lonlat(0), lonlat(100), costs, 160)
        self.assertEqual(result, again)
        self.assertEqual(dataset, before)
        self.assertEqual(costs, before_costs)


if __name__ == "__main__":
    unittest.main()
