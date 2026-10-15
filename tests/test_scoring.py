"""Numerical examples of the saved GH formulas and the explicit new adapter.

The original graph uses 0..1 weights. The 1..10 profile conversion and
zero-weight fallback are migration policies, not recovered astrology rules.
"""

import math
import unittest

from luckypath.scoring import Targets, Weights, evaluate_edge


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.features = {"open": 0.4, "green": 0.5, "still": 0.9,
                         "social": 0.2, "access": 0.9}

    def test_saved_defaults_match_hand_calculated_gh_example(self):
        # GH saved values: weights .25/.8/.05, all fit targets .8, alpha 1.
        # open fit=.6, weighted numerator=.595, denominator=1.1.
        # For 100 m, spatial fit=119/220 and total cost=1605/11.
        result = evaluate_edge(self.features, 100, Weights(), Targets())
        self.assertEqual(Weights().to_dict(), {
            "open": 0.25, "green": 0.8, "still": 0.05, "social": 1.0, "access": 1.0,
        })
        self.assertAlmostEqual(result["fits"]["open"], 0.6)
        self.assertAlmostEqual(result["fits"]["green"], 0.5)
        self.assertAlmostEqual(result["fits"]["still"], 0.9)
        self.assertAlmostEqual(result["fits"]["social"], 0.4)
        self.assertAlmostEqual(result["fits"]["access"], 0.9)
        self.assertAlmostEqual(result["spatial_fit"], 119 / 220)
        self.assertAlmostEqual(result["cost"], 1605 / 11)
        self.assertAlmostEqual(result["potential_fit"], 0.65)

    def test_openness_prefers_the_target_not_the_largest_measurement(self):
        weights = Weights(open=1, green=0, still=0)
        results = [evaluate_edge({**self.features, "open": value}, 100, weights, Targets())
                   for value in (0.6, 0.8, 1.0)]
        self.assertAlmostEqual(results[0]["cost"], 120)
        self.assertAlmostEqual(results[1]["cost"], 100)
        self.assertAlmostEqual(results[2]["cost"], 120)

    def test_social_access_preferences_change_potential_but_not_route_cost(self):
        social = evaluate_edge(self.features, 100, Weights(social=1, access=0), Targets())
        access = evaluate_edge(self.features, 100, Weights(social=0, access=1), Targets())
        self.assertAlmostEqual(social["potential_fit"], 0.4)
        self.assertAlmostEqual(access["potential_fit"], 0.9)
        self.assertEqual(social["cost"], access["cost"])
        self.assertEqual(social["spatial_fit"], access["spatial_fit"])
        changed = evaluate_edge({**self.features, "social": 0.8, "access": 0.1},
                                100, Weights(), Targets(social=0.2, access=1))
        self.assertEqual(changed["cost"], social["cost"])

    def test_new_profile_adapter_maps_one_to_zero_and_ten_to_one(self):
        keys = ("open", "green", "still", "social", "access")
        low = Weights.from_profile_scores(dict.fromkeys(keys, 1))
        high = Weights.from_profile_scores(dict.fromkeys(keys, 10))
        self.assertEqual(low.to_dict(), dict.fromkeys(keys, 0.0))
        self.assertEqual(high.to_dict(), dict.fromkeys(keys, 1.0))
        mixed = Weights.from_profile_scores({
            "open": 1, "green": 10, "still": 5.5, "social": 4, "access": 7,
        })
        self.assertEqual(mixed.still, 0.5)
        self.assertAlmostEqual(mixed.social, 1 / 3)
        self.assertAlmostEqual(mixed.access, 2 / 3)

    def test_profile_adapter_requires_all_five_keys_and_valid_scale(self):
        valid = dict.fromkeys(("open", "green", "still", "social", "access"), 5)
        for profile in ({"open": 5}, {**valid, "extra": 5},
                        {**valid, "open": 0}, {**valid, "open": 11},
                        {**valid, "open": math.nan}, {**valid, "open": True}):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                Weights.from_profile_scores(profile)

    def test_zero_spatial_weights_explicitly_fall_back_to_length(self):
        result = evaluate_edge(self.features, 123, Weights(open=0, green=0, still=0), Targets())
        self.assertEqual(result["cost"], 123)
        self.assertIsNone(result["spatial_fit"])
        self.assertAlmostEqual(result["potential_fit"], 0.65)
        no_potential = evaluate_edge(self.features, 123, Weights(social=0, access=0), Targets())
        self.assertIsNone(no_potential["potential_fit"])
        self.assertIsNotNone(no_potential["spatial_fit"])

    def test_edge_cost_is_total_cost_and_scales_with_length(self):
        short = evaluate_edge(self.features, 20, Weights(), Targets())
        long = evaluate_edge(self.features, 100, Weights(), Targets())
        self.assertAlmostEqual(long["cost"], 5 * short["cost"])
        length_only = evaluate_edge(self.features, 100, Weights(), Targets(), alpha=0)
        self.assertEqual(length_only["cost"], 100)

    def test_nonfinite_and_out_of_range_inputs_are_rejected(self):
        for invalid in (-0.01, 1.01, math.nan, math.inf, True, "0.5"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                evaluate_edge({**self.features, "green": invalid}, 100, Weights(), Targets())
            with self.subTest(weight=invalid), self.assertRaises(ValueError):
                Weights(open=invalid)
        for length in (-1, math.nan, math.inf, True):
            with self.subTest(length=length), self.assertRaises(ValueError):
                evaluate_edge(self.features, length, Weights(), Targets())
        with self.assertRaises(ValueError):
            Weights.from_dict({"unknown": 0.5})
        with self.assertRaises(ValueError):
            Targets.from_dict({"green": 0.5})


if __name__ == "__main__":
    unittest.main()
