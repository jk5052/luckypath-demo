"""Behavioural checks for network connectivity, splitting, and weighted routes."""

import copy
import json
import math
from types import SimpleNamespace
import unittest

from pyproj import Transformer

from luckypath.routing import RoutingError, plan_route


_TO_LONLAT = Transformer.from_crs(32618, 4326, always_xy=True)
_TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)
_X, _Y = _TO_METRES.transform(-73.96, 40.715)


def xy(x, y=0):
    return _X + x, _Y + y


def lonlat(x, y=0):
    return _TO_LONLAT.transform(*xy(x, y))


def edge(edge_id, u, v, coords):
    return SimpleNamespace(id=edge_id, u=u, v=v, coords=coords,
                           length=sum(math.dist(a, b) for a, b in zip(coords, coords[1:])),
                           osm_tags={})


def single_road(coords=None):
    coords = coords or [xy(0), xy(100)]
    return SimpleNamespace(nodes={"a": coords[0], "b": coords[-1]},
                           edges=[edge("ab", "a", "b", coords)])


class RoutingTests(unittest.TestCase):
    def assert_route_xy(self, result, expected):
        actual = result["route"]["geometry"]["coordinates"]
        self.assertEqual(len(actual), len(expected))
        for coord, wanted in zip(actual, expected):
            projected = _TO_METRES.transform(*coord)
            self.assertAlmostEqual(projected[0], xy(*wanted)[0], places=5)
            self.assertAlmostEqual(projected[1], xy(*wanted)[1], places=5)

    def test_same_edge_uses_only_travelled_fraction_and_preserves_dataset(self):
        dataset = single_road()
        before = copy.deepcopy(dataset)
        costs = {"ab": 20.0}
        result = plan_route(dataset, lonlat(20, 3), lonlat(80, -4), costs)
        self.assertAlmostEqual(result["length_m"], 60, places=5)
        self.assertAlmostEqual(result["total_cost"], 12, places=5)
        self.assertAlmostEqual(result["snaps"]["origin"]["distance_m"], 3, places=5)
        self.assertEqual(result["edge_ids"], ["ab"])
        self.assertEqual(dataset, before)
        self.assertEqual(costs, {"ab": 20.0})
        self.assert_route_xy(result, [(20, 0), (80, 0)])
        json.dumps(result, allow_nan=False)

    def test_same_edge_via_behind_destination_preserves_backtracking(self):
        result = plan_route(single_road(), lonlat(20), lonlat(50), {"ab": 20},
                            via_lonlat=lonlat(80))
        self.assertAlmostEqual(result["length_m"], 90, places=5)
        self.assertAlmostEqual(result["total_cost"], 18, places=5)
        self.assert_route_xy(result, [(20, 0), (50, 0), (80, 0), (50, 0)])
        self.assertIn("snapped_via", result)

    def test_curved_road_keeps_vertices_in_reverse_direction(self):
        dataset = single_road([xy(0), xy(0, 100), xy(100, 100)])
        result = plan_route(dataset, lonlat(75, 100), lonlat(0, 25), {"ab": 40})
        self.assertAlmostEqual(result["length_m"], 150, places=5)
        self.assertAlmostEqual(result["total_cost"], 30, places=5)
        self.assert_route_xy(result, [(75, 100), (0, 100), (0, 25)])

    def test_lower_cost_detour_wins_even_when_longer(self):
        dataset = SimpleNamespace(
            nodes={"a": xy(0), "b": xy(100), "c": xy(0, 100), "d": xy(100, 100)},
            edges=[edge("ab", "a", "b", [xy(0), xy(100)]),
                   edge("ac", "a", "c", [xy(0), xy(0, 100)]),
                   edge("cd", "c", "d", [xy(0, 100), xy(100, 100)]),
                   edge("db", "d", "b", [xy(100, 100), xy(100)])],
        )
        result = plan_route(dataset, lonlat(0), lonlat(100),
                            {"ab": 100, "ac": 1, "cd": 1, "db": 1})
        self.assertEqual(result["edge_ids"], ["ac", "cd", "db"])
        self.assertAlmostEqual(result["length_m"], 300, places=5)
        self.assertAlmostEqual(result["total_cost"], 3, places=5)

    def test_zero_cost_roads_are_usable(self):
        result = plan_route(single_road(), lonlat(10), lonlat(90), {"ab": 0})
        self.assertEqual(result["total_cost"], 0)
        self.assertAlmostEqual(result["length_m"], 80, places=5)

    def test_identical_points_return_zero_length(self):
        result = plan_route(single_road(), lonlat(40), lonlat(40), {"ab": 50})
        self.assertEqual(result["length_m"], 0)
        self.assertEqual(result["total_cost"], 0)
        self.assertEqual(result["edge_ids"], [])
        self.assert_route_xy(result, [(40, 0), (40, 0)])

    def test_crossing_lines_without_shared_nodes_stay_disconnected(self):
        dataset = SimpleNamespace(
            nodes={"a": xy(-100), "b": xy(100), "c": xy(0, -100), "d": xy(0, 100)},
            edges=[edge("ab", "a", "b", [xy(-100), xy(100)]),
                   edge("cd", "c", "d", [xy(0, -100), xy(0, 100)])],
        )
        with self.assertRaisesRegex(RoutingError, "No connected walking route"):
            plan_route(dataset, lonlat(-50), lonlat(0, 50), {"ab": 1, "cd": 1})

    def test_coincident_endpoints_with_different_ids_are_not_merged(self):
        dataset = SimpleNamespace(
            nodes={"a": xy(0), "b": xy(100), "c": xy(100), "d": xy(200)},
            edges=[edge("ab", "a", "b", [xy(0), xy(100)]),
                   edge("cd", "c", "d", [xy(100), xy(200)])],
        )
        with self.assertRaisesRegex(RoutingError, "No connected walking route"):
            plan_route(dataset, lonlat(20), lonlat(180), {"ab": 1, "cd": 1})

    def test_disconnected_via_does_not_return_direct_origin_destination_route(self):
        dataset = single_road()
        dataset.nodes.update({"c": xy(0, 100), "d": xy(100, 100)})
        dataset.edges.append(edge("cd", "c", "d", [xy(0, 100), xy(100, 100)]))
        with self.assertRaisesRegex(RoutingError, "origin to via"):
            plan_route(dataset, lonlat(10), lonlat(90), {"ab": 1, "cd": 1},
                       via_lonlat=lonlat(50, 100))

    def test_invalid_costs_fail_explicitly(self):
        for cost in (-1, float("nan"), float("inf"), -float("inf"), None, True):
            with self.subTest(cost=cost), self.assertRaises(RoutingError):
                plan_route(single_road(), lonlat(10), lonlat(90), {"ab": cost})
        with self.assertRaisesRegex(RoutingError, "Missing cost"):
            plan_route(single_road(), lonlat(10), lonlat(90), {})

    def test_far_points_and_invalid_inputs_fail_explicitly(self):
        with self.assertRaisesRegex(RoutingError, "maximum snap distance"):
            plan_route(single_road(), lonlat(10, 151), lonlat(90), {"ab": 1})
        for coord in (None, [1], [181, 40], [0, 91], [float("nan"), 40],
                      {"lon": -73.96, "lat": 40.715}, "bad"):
            with self.subTest(coord=coord), self.assertRaises(RoutingError):
                plan_route(single_road(), coord, lonlat(90), {"ab": 1})
        for distance in (-1, float("nan"), float("inf")):
            with self.subTest(distance=distance), self.assertRaises(RoutingError):
                plan_route(single_road(), lonlat(10), lonlat(90), {"ab": 1},
                           max_snap_m=distance)

    def test_empty_network_fails_explicitly(self):
        with self.assertRaisesRegex(RoutingError, "no roads"):
            plan_route(SimpleNamespace(nodes={}, edges=[]), lonlat(10), lonlat(90), {})


if __name__ == "__main__":
    unittest.main()
