"""Analytic geometry fixtures for the recovered spatial measurements.

These are small independently understandable maps, not captured production
outputs. The same-edge waterfront test records an intentional GH bug repair.
"""

import math
from types import SimpleNamespace
import unittest

from shapely.geometry import LineString, box

from luckypath.features import (
    linearity, major_road_distances, minmax, nearby_aggregates, openness,
    percentile, prepare_features, waterfront_distances,
)


def road(edge_id, u, v, coords, **tags):
    return SimpleNamespace(id=edge_id, u=u, v=v, coords=coords,
                           length=LineString(coords).length, osm_tags=tags)


class FeatureTests(unittest.TestCase):
    def test_minmax_is_dataset_wide_and_constant_field_is_neutral(self):
        self.assertEqual(minmax([2, 4, 10]), [0, 0.25, 1])
        self.assertEqual(minmax([0, 0, 0]), [0.5, 0.5, 0.5])
        self.assertEqual(minmax([]), [])
        with self.assertRaises(ValueError):
            minmax([1, math.nan])

    def test_percentiles_use_average_ranks_for_ties_and_keep_original_order(self):
        self.assertEqual(percentile([100, 50, 50, 0]), [1, 0.5, 0.5, 0])
        self.assertEqual(percentile([7, 7, 7]), [0.5, 0.5, 0.5])
        self.assertEqual(percentile([42]), [0.5])
        self.assertEqual(percentile([]), [])

    def test_neighbourhood_is_a_circle_in_metres_not_a_bounding_box(self):
        # (49,49) is inside the 50 m square but outside the 50 m circle.
        # (30,40) lies exactly on the radius and must be included.
        points = [(0, 0), (30, 40), (49, 49), (50.01, 0)]
        self.assertEqual(nearby_aggregates([(0, 0)], points, radius=50), [2])
        self.assertEqual(nearby_aggregates([(0, 0)], points, [2, 3, 100, 200], 50), [5])
        self.assertEqual(nearby_aggregates([(0, 0)], [], radius=50), [0])
        with self.assertRaises(ValueError):
            nearby_aggregates([(0, 0)], points, [1], 50)

    def test_openness_uses_nearest_intersection_of_eight_200_metre_rays(self):
        # Only the east ray encounters either narrow wall; first hit is 10 m.
        # The other seven rays contribute their full 200 m cap.
        buildings = [box(10, -1, 11, 1), box(20, -1, 21, 1)]
        self.assertAlmostEqual(openness([(0, 0)], buildings, radius=200)[0], 0.88125)
        self.assertEqual(openness([(0, 0)], [box(201, -1, 202, 1)], radius=200), [1])

    def test_diagonal_rays_use_euclidean_distance(self):
        # Four cardinal rays meet the square at 10 m, four diagonal rays at
        # 10*sqrt(2) m. The enclosure is an analytic geometry fixture.
        expected = (1 + math.sqrt(2)) / 40
        self.assertAlmostEqual(openness([(0, 0)], [box(-10, -10, 10, 10)], radius=200)[0],
                               expected)

    def test_missing_buildings_and_invalid_radii_are_explicit_errors(self):
        with self.assertRaises(ValueError):
            openness([(0, 0)], [], radius=200)
        for radius in (0, -1, math.inf, math.nan):
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                openness([(0, 0)], [box(10, -1, 11, 1)], radius=radius)

    def test_linearity_uses_endpoint_continuations_and_is_orientation_invariant(self):
        edges = [road("ab", "a", "b", [(0, 0), (100, 0)]),
                 road("cb", "c", "b", [(200, 0), (100, 0)]),
                 road("cd", "c", "d", [(200, 0), (300, 0)])]
        self.assertEqual(linearity(edges), [0.5, 1.0, 0.5])
        right_angle = [edges[0], road("be", "b", "e", [(100, 0), (100, 100)])]
        self.assertEqual(linearity(right_angle), [0, 0])
        separate_node = [edges[0], road("ef", "e", "f", [(100, 0), (200, 0)])]
        self.assertEqual(linearity(separate_node), [0, 0])

    def test_stillness_distance_uses_major_road_geometry_including_link_tags(self):
        edges = [road("major", "a", "b", [(0, -100), (0, 100)], highway="primary_link"),
                 road("minor", "c", "d", [(9, -100), (9, 100)], highway="residential")]
        self.assertEqual(major_road_distances([(10, 0), (25, 0)], edges), [10, 25])
        with self.assertRaises(ValueError):
            major_road_distances([(10, 0)], [edges[1]])

    def test_waterfront_same_edge_direct_distance_repairs_source_overestimate(self):
        # GH script_08 only checked endpoint paths and would report 90 m.
        # Midpoint 50 -> entrance 60 is a direct 10 m path on that same road.
        edges = [road("ab", "a", "b", [(0, 0), (100, 0)]),
                 road("bc", "b", "c", [(100, 0), (200, 0)]),
                 road("de", "d", "e", [(1000, 0), (1100, 0)])]
        self.assertEqual(waterfront_distances(edges, [(60, 0)]), [10, 90, None])
        self.assertEqual(waterfront_distances(edges[:1], [(60, 151)]), [None])

    def test_default_feature_pipeline_uses_saved_radii_and_separate_components(self):
        edges = [road(str(i), f"{i}a", f"{i}b", [(x - 10, 0), (x + 10, 0)])
                 for i, x in enumerate((0, 1000, 2000))]
        dataset = SimpleNamespace(
            edges=edges,
            trees=[(30, 40), (49, 49), (1000, 0), (1030, 40)],
            places=[{"x": 0, "y": 0, "social_component": 2, "access_component": 1},
                    {"x": 99, "y": 99, "social_component": 100, "access_component": 100},
                    {"x": 1000, "y": 0, "social_component": 3, "access_component": 2}],
            buildings=[box(100, -1, 101, 1)],
            major_roads=[LineString([(0, -1000), (0, 1000)])],
            entrances=[(0, 0)],
        )
        result = prepare_features(dataset)
        values = [result["edges"][str(i)] for i in range(3)]
        self.assertEqual([value["trees_within_radius"] for value in values], [1, 2, 0])
        self.assertEqual([value["green"] for value in values], [0.5, 1, 0])
        self.assertEqual([value["social_raw"] for value in values], [2, 3, 0])
        self.assertEqual([value["access_raw"] for value in values], [1, 2, 0])
        self.assertEqual([value["social"] for value in values], [2 / 3, 1, 0])
        self.assertEqual([value["access"] for value in values], [0.5, 1, 0])
        self.assertEqual([value["still"] for value in values], [0, 0.5, 1])
        self.assertEqual([value["waterfront_distance_m"] for value in values], [0, None, None])
        self.assertAlmostEqual(values[0]["open"], 0.9375)
        self.assertEqual(result["parameters"]["tree_radius_m"], 50)
        self.assertEqual(result["parameters"]["poi_radius_m"], 100)
        self.assertEqual(result["parameters"]["open_radius_m"], 200)


if __name__ == "__main__":
    unittest.main()
