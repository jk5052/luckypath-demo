"""Independent provider, uncertainty, source provenance and inspectable adapter."""
import json
import math
import socket
import unittest
from unittest.mock import patch

from luckypath.analysis_input import prepare_input
from luckypath.astrology_analysis import analyze_astrology, _profile, PLANETS, KEYS


BASE = {"birth_date": "1990-05-15", "birth_time": "14:30", "birth_timezone": "Asia/Seoul",
        "target_date": "2026-10-13", "birth_latitude": 37.5665, "birth_longitude": 126.978}


def analyze(**changes):
    return analyze_astrology(prepare_input({**BASE, **changes}))


class AstrologyAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze()

    def test_real_ten_planets_and_reviewed_bundled_jpl_source(self):
        result = self.result
        provider = result["provider"]
        self.assertEqual((provider["version"], provider["backend_version"]), ("6.0.5", "3.2.2"))
        self.assertEqual((provider["backend"], provider["mode"], provider["network_policy"]),
                         ("libephemeris", "leb", "sealed"))
        self.assertEqual(provider["data"]["artifact"], "base_core.leb2")
        self.assertEqual(provider["data"]["sha256"], "b891de02f82b68346046da67389fd3713aa81ab299287c632cb20cc396397964")
        planets = result["analysis"]["natal"]["planets"]
        self.assertEqual(set(planets), set(PLANETS))
        self.assertAlmostEqual(planets["Sun"]["longitude_deg"], 54.134953309223484, places=7)
        self.assertAlmostEqual(planets["Moon"]["longitude_deg"], 293.8243176574886, places=7)
        for point in planets.values():
            self.assertEqual(point["source"], "LEB")
            self.assertTrue(point["source_reviewed"])
            self.assertEqual(point["retrograde"], point["speed_deg_per_day"] < 0)

    def test_aspects_use_actual_longitude_and_today_to_fixed_natal_direction(self):
        analysis = self.result["analysis"]
        self.assertTrue(analysis["transits"])
        for key in ("natal", "today"):
            planets = analysis[key]["planets"]
            for aspect in analysis[key]["aspects"]:
                delta = abs((planets[aspect["first"]]["longitude_deg"] -
                             planets[aspect["second"]]["longitude_deg"] + 180) % 360 - 180)
                self.assertAlmostEqual(abs(delta - aspect["angle_deg"]), aspect["orb_deg"], places=9)
        for aspect in analysis["transits"]:
            point = analysis["today"]["planets"][aspect["first"]]
            today = point["longitude_deg"]
            natal = analysis["natal"]["planets"][aspect["second"]]["longitude_deg"]
            delta = abs((today - natal + 180) % 360 - 180)
            self.assertAlmostEqual(abs(delta - aspect["angle_deg"]), aspect["orb_deg"], places=9)
            self.assertLessEqual(aspect["orb_deg"], 3.)
            # Advance only the moving planet one second. The natal speed must
            # not enter a transit's applying/separating classification.
            later = abs((today + point["speed_deg_per_day"] / 86400 - natal + 180) % 360 - 180)
            expected = "Applying" if abs(later - aspect["angle_deg"]) < aspect["orb_deg"] else "Separating"
            self.assertEqual(aspect["movement"], expected)
        self.assertEqual(analysis["transit_direction"], "today moving → natal fixed")

    def test_real_location_changes_houses_but_not_geocentric_planets(self):
        different = analyze(birth_latitude=40.7128, birth_longitude=-74.006)
        first, second = self.result["analysis"]["natal"], different["analysis"]["natal"]
        self.assertEqual(len(first["houses"]), 12)
        self.assertNotEqual(first["angles"]["ascendant"], second["angles"]["ascendant"])
        for name in PLANETS:
            self.assertEqual(first["planets"][name]["longitude_deg"], second["planets"][name]["longitude_deg"])
        self.assertIsNotNone(first["planets"]["Venus"]["essential_dignity"])

    def test_no_coordinates_means_no_fake_houses_angles_or_dignities(self):
        payload = {key: value for key, value in BASE.items() if key not in ("birth_latitude", "birth_longitude")}
        result = analyze_astrology(prepare_input(payload))
        natal = result["analysis"]["natal"]
        self.assertIsNone(natal["houses"])
        self.assertIsNone(natal["angles"])
        for point in natal["planets"].values():
            self.assertIsNone(point["house"])
            self.assertIsNone(point["dignity_score"])
        self.assertTrue(natal["aspects"])
        self.assertTrue(result["analysis"]["transits"])
        self.assertNotIn("Greenwich", json.dumps(result))

    def test_whole_sign_is_explicit_and_does_not_change_planetary_weights(self):
        whole = analyze(house_system="W")
        natal = whole["analysis"]["natal"]
        self.assertEqual(natal["house_system"], "W")
        self.assertTrue(all(abs(house["longitude_deg"] % 30) < 1e-9 for house in natal["houses"]))
        self.assertEqual(whole["profile"], self.result["profile"])

    def test_polar_house_substitution_is_reported_without_raw_location(self):
        result = analyze(birth_latitude=78.2232, birth_longitude=15.6469)
        natal = result["analysis"]["natal"]
        self.assertTrue(natal["house_fallbacks"])
        self.assertEqual(natal["house_fallbacks"][0]["requested"], "P")
        self.assertEqual(natal["house_system"], natal["house_fallbacks"][0]["used"])
        self.assertTrue(any(item["field"] == "natal.house_system" for item in result["omissions"]))
        self.assertNotIn("78.2232", json.dumps(result))

    def test_unknown_time_preserves_uncertainty_and_never_uses_noon_as_birth(self):
        result = analyze(birth_date="2000-03-20", birth_time=None, birth_timezone="America/New_York")
        natal = result["analysis"]["natal"]
        self.assertFalse(natal["time_known"])
        self.assertIsNone(natal["houses"])
        self.assertIsNone(natal["angles"])
        self.assertEqual(natal["aspects"], [])
        self.assertEqual(result["analysis"]["transits"], [])
        self.assertIsNone(natal["planets"]["Sun"]["sign"])
        self.assertEqual(natal["planets"]["Sun"]["alternatives"], ["물고기자리", "양자리"])
        self.assertTrue(all(item["longitude_deg"] is None for item in natal["planets"].values()))
        self.assertFalse(any(item["id"] == "natal-Sun" for item in result["profile"]["evidence"]))
        self.assertTrue(any(item["id"] == "today-Moon" for item in result["profile"]["evidence"]))
        self.assertEqual(result["profile"]["status"], "ready")

    def test_explicit_experimental_score_can_be_recomputed_from_evidence(self):
        profile = self.result["profile"]
        self.assertTrue(profile["policy"]["experimental"])
        self.assertFalse(profile["policy"]["externally_validated"])
        self.assertTrue(any(item["rule_id"] == "transit-tight-major-aspect" for item in profile["evidence"]))
        total = sum(item["weight"] for item in profile["evidence"])
        for key in KEYS:
            expected = sum(item["weight"] * item["vector"][key] for item in profile["evidence"]) / total
            self.assertAlmostEqual(profile["score_details"][key]["unrounded"], expected)
            self.assertEqual(profile["profile_scores"][key], math.floor(expected + .5))
            self.assertTrue(1 <= profile["profile_scores"][key] <= 10)
        self.assertEqual(len({item["id"] for item in profile["evidence"]}), len(profile["evidence"]))

    def test_hand_computed_adapter_and_tight_orb_exclusion(self):
        # Only natal Sun/Fire and today's Moon/Water: vectors
        # (8.5,4.5,3.5,7.5,5.5) and (4,8,9,4,6), both weight 1.
        natal = {"planets": {name: {"element": None, "retrograde": False} for name in PLANETS}}
        natal["planets"]["Sun"].update(element="Fire", sign="양자리")
        today = {"planets": {name: {"retrograde": False} for name in PLANETS}}
        today["planets"]["Moon"].update(element="Water", sign="게자리")
        today["planets"]["Mercury"]["retrograde"] = True
        baseline = _profile(natal, today, [], [])
        self.assertEqual([baseline["score_details"][key]["unrounded"] for key in KEYS],
                         [6.25, 6.25, 6.25, 5.75, 5.75])
        aspect = {"first": "Mercury", "second": "Sun", "aspect": "square", "label": "사각",
                  "orb_deg": 1.5, "movement": "Applying"}
        with_transit = _profile(natal, today, [], [aspect])
        # Square and retrograde produce (7,4.5,7,6.5,8), weight .5.
        expected = [6.4, 5.9, 6.4, 5.9, 6.2]
        for key, value in zip(KEYS, expected):
            self.assertAlmostEqual(with_transit["score_details"][key]["unrounded"], value)
        self.assertEqual(_profile(natal, today, [], [{**aspect, "orb_deg": 3.}]), baseline)

    def test_birth_and_target_both_affect_independent_analysis(self):
        different_birth = analyze(birth_date="2001-08-03")
        different_day = analyze(target_date="2026-11-01")
        self.assertNotEqual(different_birth["analysis"]["transits"], self.result["analysis"]["transits"])
        self.assertNotEqual(different_day["analysis"]["transits"], self.result["analysis"]["transits"])
        self.assertNotEqual(different_birth["profile"]["score_details"], self.result["profile"]["score_details"])
        self.assertNotEqual(different_day["profile"]["score_details"], self.result["profile"]["score_details"])

    def test_no_network_and_no_raw_birth_fields_in_public_result(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network")):
            result = analyze()
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        for value in ("1990-05-15", "14:30", "Asia/Seoul", "37.5665", "126.978", "birth_latitude", "birth_longitude"):
            self.assertNotIn(value, text)
        self.assertEqual(result, self.result)

    def test_provider_exceptions_are_sanitized(self):
        with patch("luckypath.astrology_analysis._subject", side_effect=RuntimeError("private 1990-05-15")):
            with self.assertRaises(ValueError) as error:
                analyze()
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("1990", str(error.exception))


if __name__ == "__main__":
    unittest.main()
