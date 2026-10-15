"""Check the published policy's arithmetic and contract, not astrological truth."""
import copy
import json
import math
import unittest

from luckypath.astro_policy import (
    BASE_WEIGHTS, POLICY_ID, SAJU_VECTORS, ZODIAC_VECTORS,
    interpret_profile, policy_definition, validate_policy,
)


def pillar(stem, branch=None):
    return {"stem_element": stem, "branch_element": branch or stem}


def chart(element=None, sun=None, moon=None):
    result = {"pillars": {"day": pillar(element)} if element else {}}
    if sun:
        result["sun"] = {"element": sun}
    if moon:
        result["moon"] = {"element": moon}
    return result


class AstroPolicyTests(unittest.TestCase):
    def test_full_profile_matches_hand_calculated_four_component_blend(self):
        result = interpret_profile(chart("wood", "fire", "fire"),
                                   chart("earth", "air", "air"))
        self.assertEqual(result["profile_scores"], {
            "open": 7, "green": 7, "still": 4, "social": 8, "access": 6,
        })
        expected = {"open": 7.15, "green": 6.55, "still": 4.1,
                    "social": 7.85, "access": 5.75}
        for key, total in expected.items():
            self.assertAlmostEqual(result["score_details"][key]["unrounded"], total)
            parts = result["score_details"][key]["contributions"]
            self.assertAlmostEqual(sum(value["weighted_value"] for value in parts.values()), total)
        self.assertEqual(result["policy"]["effective_weights"],
                         {key: float(value) for key, value in BASE_WEIGHTS.items()})
        self.assertEqual(result["policy"]["id"], POLICY_ID)
        self.assertFalse(result["policy"]["externally_validated"])

    def test_each_available_stem_and_branch_has_equal_weight(self):
        natal = {"pillars": {"year": pillar("wood"), "month": pillar("fire", "metal"),
                             "day": pillar("earth"), "hour": pillar("water")}}
        result = interpret_profile(natal, None)
        details = result["component_details"]["natal_saju"]
        self.assertEqual(details["signal_count"], 8)
        self.assertEqual(len(details["signals"]), 8)
        # open = (6+6+8+9+5+5+6+6)/8; no day/month special weight.
        self.assertEqual(result["score_details"]["open"]["unrounded"], 51 / 8)
        self.assertEqual(result["policy"]["effective_weights"]["natal_saju"], 1)

    def test_daily_saju_uses_only_the_day_pillar(self):
        today = {"pillars": {"day": pillar("water"), "year": pillar("fire"),
                             "month": pillar("metal"), "hour": pillar("wood")}}
        result = interpret_profile(None, today)
        self.assertEqual(result["profile_scores"], dict(zip(
            ("open", "green", "still", "social", "access"), SAJU_VECTORS["water"])))
        self.assertEqual(result["component_details"]["daily_saju"]["signal_count"], 2)

    def test_sun_and_moon_have_equal_weight_in_each_zodiac_component(self):
        for natal, today, component in (
            (chart(sun="fire", moon="earth"), None, "natal_zodiac"),
            (None, chart(sun="fire", moon="earth"), "daily_zodiac"),
        ):
            with self.subTest(component=component):
                result = interpret_profile(natal, today)
                self.assertEqual(result["component_details"][component]["signal_count"], 2)
                self.assertEqual(result["score_details"]["open"]["unrounded"], 6.5)
                self.assertEqual(result["profile_scores"]["open"], 7)
                self.assertEqual(result["profile_scores"]["green"], 7)

    def test_missing_whole_components_renormalize_existing_weights(self):
        result = interpret_profile(chart("wood"), chart(sun="air"))
        self.assertEqual(result["policy"]["effective_weights"], {
            "natal_saju": .7, "natal_zodiac": 0, "daily_saju": 0, "daily_zodiac": .3,
        })
        self.assertEqual(result["profile_scores"], {
            "open": 7, "green": 8, "still": 5, "social": 7, "access": 5,
        })
        parts = result["score_details"]["open"]["contributions"]
        self.assertIsNone(parts["natal_zodiac"]["mean"])
        self.assertEqual(parts["natal_zodiac"]["weighted_value"], 0)
        self.assertEqual(result["policy"]["missing_components"], ["natal_zodiac", "daily_saju"])
        self.assertIn("natal_zodiac", {item["field"] for item in result["omissions"]})

    def test_missing_moon_is_excluded_without_fake_neutral_score(self):
        result = interpret_profile(chart(sun="water"), None)
        self.assertEqual(result["profile_scores"], {
            "open": 5, "green": 7, "still": 10, "social": 4, "access": 5,
        })
        self.assertEqual(result["component_details"]["natal_zodiac"]["signal_count"], 1)
        self.assertIn("natal.moon", {item["field"] for item in result["omissions"]})

    def test_missing_birth_time_does_not_select_an_alternative(self):
        natal = chart("wood", sun="air")
        natal["pillars"]["hour"] = None
        natal["moon"] = None
        natal["alternatives"] = {"pillars": {"hour": [pillar("fire")]},
                                 "moon_signs": [{"element": "fire"}, {"element": "earth"}]}
        result = interpret_profile(natal, None)
        self.assertEqual(result["component_details"]["natal_saju"]["signal_count"], 2)
        self.assertEqual(result["component_details"]["natal_zodiac"]["signal_count"], 1)
        fields = {item["field"] for item in result["omissions"]}
        self.assertTrue({"natal.hour", "natal.moon"}.issubset(fields))

    def test_rounding_is_half_up_not_bankers_rounding(self):
        natal = {"pillars": {"day": pillar("wood", "fire")}}
        result = interpret_profile(natal, None)
        self.assertEqual(result["score_details"]["still"]["unrounded"], 3.5)
        self.assertEqual(result["profile_scores"]["still"], 4)
        result = interpret_profile(chart(sun="fire", moon="earth"), None)
        self.assertEqual(result["score_details"]["open"]["unrounded"], 6.5)
        self.assertEqual(result["profile_scores"]["open"], 7)

    def test_no_signals_is_an_error_not_a_manufactured_profile(self):
        for natal, today in ((None, None), ({}, {}), ({"pillars": {}}, {"sun": None})):
            with self.subTest(natal=natal, today=today), self.assertRaisesRegex(ValueError, "No available"):
                interpret_profile(natal, today)

    def test_unknown_elements_and_malformed_charts_fail_explicitly(self):
        invalid = [[], {"pillars": []}, {"pillars": {"day": "wood"}},
                   {"sun": "fire"}, {"moon": {"element": "wood"}},
                   {"pillars": {"day": pillar("air")}}, {"sun": {"element": 1}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                interpret_profile(value, chart("wood", "fire"))

    def test_policy_tables_validate_shape_scale_and_weights(self):
        validate_policy()
        for bad_vector in ((1, 2), (True, 4, 2, 10, 6), (math.nan, 4, 2, 10, 6),
                           (math.inf, 4, 2, 10, 6), (11, 4, 2, 10, 6), (0, 4, 2, 10, 6)):
            with self.subTest(vector=bad_vector), self.assertRaises(ValueError):
                validate_policy(saju_vectors={**SAJU_VECTORS, "fire": bad_vector})
        with self.assertRaises(ValueError):
            validate_policy(zodiac_vectors={**ZODIAC_VECTORS, "metal": (5, 5, 5, 5, 5)})
        with self.assertRaises(ValueError):
            validate_policy(weights={**BASE_WEIGHTS, "natal_saju": 0})
        with self.assertRaises(ValueError):
            validate_policy(weights={**BASE_WEIGHTS, "natal_saju": .4})

    def test_results_are_deterministic_json_safe_and_do_not_mutate_inputs(self):
        natal, today = chart("wood", "earth", "water"), chart("fire", "air", "fire")
        before = copy.deepcopy((natal, today))
        first = interpret_profile(natal, today)
        second = interpret_profile(natal, today)
        self.assertEqual(first, second)
        self.assertEqual((natal, today), before)
        self.assertTrue(json.dumps(first, allow_nan=False))
        first["policy"]["saju_vectors"]["wood"]["green"] = 1
        first["policy"]["base_weights"]["natal_saju"] = 0
        self.assertEqual(interpret_profile(natal, today), second)
        self.assertEqual(policy_definition()["saju_vectors"]["wood"]["green"], 10)


if __name__ == "__main__":
    unittest.main()
