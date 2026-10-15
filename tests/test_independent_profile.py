"""The only cross-system operation is an explained mean of experience weights."""
import copy
import json
from types import ModuleType
import unittest
from unittest.mock import patch

from luckypath.independent_profile import combine_profiles, build_independent_profile
from luckypath.scoring import WEIGHT_KEYS


def profile(value):
    return {"status": "ready", "policy_id": "test",
            "profile_scores": {key: 5 for key in WEIGHT_KEYS},
            "score_details": {key: {"unrounded": value} for key in WEIGHT_KEYS}}


class IndependentProfileTests(unittest.TestCase):
    def test_each_analysis_has_equal_weight_and_final_rounding_occurs_once(self):
        saju, astrology = profile(4.4), profile(4.5)
        result = combine_profiles(saju, astrology)
        self.assertEqual(result["profile_scores"], dict.fromkeys(WEIGHT_KEYS, 4))
        self.assertEqual(result["score_details"]["green"]["unrounded"], 4.45)
        self.assertEqual(result["score_details"]["green"]["difference"], .1)
        result = combine_profiles(profile(4), profile(5))
        self.assertEqual(result["profile_scores"], dict.fromkeys(WEIGHT_KEYS, 5))
        self.assertEqual(result["policy"]["system_weights"], {"saju": .5, "astrology": .5})
        self.assertFalse(result["policy"]["externally_validated"])

    def test_no_silent_single_system_or_invalid_weight_fallback(self):
        for invalid in (None, {}, {"status": "unavailable"}, profile(True),
                        profile(float("nan")), profile(float("inf")), profile(11), profile(0)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                combine_profiles(profile(5), invalid)

    def test_fusion_preserves_disagreement_and_does_not_mutate_inputs(self):
        a, b = profile(1), profile(10)
        original = copy.deepcopy((a, b))
        result = combine_profiles(a, b)
        self.assertEqual((a, b), original)
        self.assertEqual(result["score_details"]["open"]["difference"], 9)
        self.assertEqual(result["score_details"]["open"]["saju"], 1)
        self.assertEqual(result["score_details"]["open"]["astrology"], 10)
        json.dumps(result, allow_nan=False)

    def test_each_domain_receives_only_shared_input_and_not_the_other_analysis(self):
        a, b = ModuleType("saju"), ModuleType("astrology")
        contexts = []
        def saju(context):
            contexts.append(copy.copy(context))
            return {"system": "saju", "secret_saju_fact": "canary", "profile": profile(4)}
        def astrology(context):
            self.assertNotIn("secret_saju_fact", context)
            contexts.append(copy.copy(context))
            return {"system": "astrology", "profile": profile(8)}
        a.analyze_saju, b.analyze_astrology = saju, astrology
        payload = {"birth_date": "1990-05-15", "birth_time": "14:30",
                   "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13"}
        with patch.dict("sys.modules", {"luckypath.saju_analysis": a, "luckypath.astrology_analysis": b}):
            result = build_independent_profile(payload)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["combined"]["profile_scores"], dict.fromkeys(WEIGHT_KEYS, 6))
        self.assertEqual(contexts[0], contexts[1])
        self.assertNotIn("profile_scores", result)
        self.assertNotIn("natal", result)

    def test_failed_provider_is_visible_and_does_not_relabel_the_other_as_combined(self):
        a, b = ModuleType("saju"), ModuleType("astrology")
        a.analyze_saju = lambda _: {"system": "saju", "profile": profile(4)}
        def failure(_):
            raise RuntimeError("private-birth-canary /Users/private")
        b.analyze_astrology = failure
        payload = {"birth_date": "1990-05-15", "birth_time": "14:30",
                   "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13"}
        with patch.dict("sys.modules", {"luckypath.saju_analysis": a, "luckypath.astrology_analysis": b}):
            result = build_independent_profile(payload)
        self.assertEqual(result["profile_status"], "unavailable")
        self.assertIsNone(result["combined"])
        self.assertEqual(result["systems"]["saju"]["profile"]["status"], "ready")
        self.assertEqual(result["systems"]["astrology"]["status"], "error")
        self.assertNotIn("private-birth-canary", json.dumps(result))

    def test_malformed_provider_contract_cannot_leak_or_produce_partial_fusion(self):
        a, b = ModuleType("saju"), ModuleType("astrology")
        a.analyze_saju = lambda _: {"system": "saju", "profile": profile(4)}
        payload = {"birth_date": "1990-05-15", "birth_time": "14:30",
                   "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13"}
        for invalid in (None, [], {"profile": None}, {"profile": profile(float("nan"))},
                        {"profile": {"status": "ready", "score_details": None}}):
            with self.subTest(invalid=invalid):
                b.analyze_astrology = lambda _, result=invalid: result
                with patch.dict("sys.modules", {"luckypath.saju_analysis": a, "luckypath.astrology_analysis": b}):
                    result = build_independent_profile(payload)
                self.assertIsNone(result["combined"])
                self.assertEqual(result["systems"]["astrology"]["status"], "error")
                json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
