"""Interpretation weights must change real routing costs, not just chart labels."""
import copy
import json
from pathlib import Path
import unittest

from luckypath.reading_routing import validate_reading_weights, evaluate_reading_edge
from luckypath.routing import plan_route, plan_budget_route
from test_budget_routing import three_alternatives, lonlat


class ReadingRoutingTests(unittest.TestCase):
    def test_vibrancy_can_select_a_different_path_and_time_cap_still_wins(self):
        dataset, _ = three_alternatives()
        # A 150m activity-rich street beats a 100m green street only when its
        # activity is preferred. Unsupported attributes cannot enter the model.
        rows = {
            "short": {"green": 1, "still": 0, "open": 0, "social": 0},
            "middle": {"green": 0, "still": 0, "open": 0, "social": 1},
            "long": {"green": 0, "still": 0, "open": 0, "social": 0},
        }
        def costs(axis):
            weights = validate_reading_weights({key: float(key == axis) for key in ("green", "quiet", "open_sky", "vibrant")})
            return {e.id: evaluate_reading_edge(rows[e.id], e.length, weights)["cost"] for e in dataset.edges}
        green = plan_route(dataset, lonlat(0), lonlat(100), costs("green"))
        social = plan_route(dataset, lonlat(0), lonlat(100), costs("vibrant"))
        self.assertEqual(green["edge_ids"], ["short"])
        self.assertEqual(social["edge_ids"], ["middle"])
        constrained = plan_budget_route(dataset, lonlat(0), lonlat(100), costs("vibrant"), 110)
        self.assertEqual(constrained["edge_ids"], ["short"])

    def test_missing_axes_are_rejected_and_costs_stay_positive(self):
        valid = dict(green=.25, quiet=.25, open_sky=.25, vibrant=.25)
        for invalid in ({}, {**valid, "water": 0}, {**valid, "green": True},
                        {**valid, "green": float("nan")}, dict.fromkeys(valid, 0),
                        {**valid, "green": .5}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_reading_weights(invalid)
        weights = validate_reading_weights(valid)
        self.assertAlmostEqual(evaluate_reading_edge(dict(green=1, still=1, open=1, social=1), 100, weights)["cost"], 60)
        with self.assertRaises(KeyError):
            evaluate_reading_edge(dict(green=1, still=1, open=1), 100, weights)


DATA = Path(__file__).resolve().parents[2] / "luckypath-nyc-data/quality/pilot/williamsburg"


@unittest.skipUnless((DATA / "dataset.json").is_file(), "Requires prepared actual streets")
class RealReadingRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from luckypath.engine import Engine
        cls.engine = Engine(DATA)
        cls.request = json.loads((DATA / "green-request.json").read_text())

    def test_exact_four_weights_reach_real_route_and_download_without_birth_inputs(self):
        request = {**copy.deepcopy(self.request), "reading_weights": dict(green=.21, quiet=.19, open_sky=.23, vibrant=.37)}
        request["time_budget"] = {"max_minutes": 30, "walking_speed_kmh": 4.5, "buffer_minutes": 3, "stop_minutes": 0}
        result = self.engine.recommend(request)
        self.assertIsNotNone(result["personalized"])
        self.assertEqual(result["routing_policy"]["reading_weights"], request["reading_weights"])
        self.assertEqual(result["geojson"]["routing_policy"], result["routing_policy"])
        self.assertEqual(result["routing_policy"]["omitted_axes"], ["water", "sunlight", "rest", "culture"])
        self.assertLessEqual(result["personalized"]["time_estimate"]["total_minutes"], 30)
        for forbidden in ("birth_date", "birth_time", "natal", "daily_reading"):
            self.assertNotIn(forbidden, json.dumps(result["geojson"]))
        with self.assertRaises(ValueError):
            self.engine.recommend({**request, "alpha": 2})


if __name__ == "__main__":
    unittest.main()
