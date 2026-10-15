"""Structural/calendar regressions and product-policy arithmetic, not predictions."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from zoneinfo import ZoneInfo

from lunar_python import Solar
from lunar_python.util import LunarUtil

from luckypath.analysis_input import prepare_input
from luckypath.saju_analysis import (
    POLICY_ID, _enrich, _make_profile, _plain_pillar, _relations, _root_evidence,
    _stage, analyze_saju, policy_definition,
)


def request(**changes):
    return {"birth_date": "1990-06-15", "birth_time": "14:30",
            "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13", **changes}


class SajuAnalysisTests(unittest.TestCase):
    def test_complete_structure_and_hand_calculated_policy_example(self):
        result = analyze_saju(prepare_input(request()))
        natal, today = result["analysis"]["natal"], result["analysis"]["today"]
        self.assertEqual([p["text"] for p in natal["pillars"].values()], ["庚午", "壬午", "辛亥", "乙未"])
        self.assertEqual(natal["day_master"]["text"], "辛")
        self.assertEqual([p["text"] for p in natal["pillars"]["hour"]["hidden_stems"]], ["己", "丁", "乙"])
        self.assertEqual(today["pillars"]["day"]["text"], "庚申")
        self.assertEqual(today["pillars"]["day"]["stem_ten_god"]["korean"], "겁재")
        self.assertEqual(result["profile"]["status"], "ready")
        # Hand calculation: natal themes have masses peer=1/7, expression=3/14,
        # wealth=11/42, authority=4/21, resource=4/21. Today's masses are
        # peer=2/3, expression=1/6, resource=1/6. Equal natal/today blend.
        expected = {"open": 29 / 7, "green": 57 / 14, "still": 65 / 14,
                    "social": 46 / 7, "access": 61 / 14}
        for key, value in expected.items():
            self.assertAlmostEqual(result["profile"]["score_details"][key]["unrounded"], value)
        self.assertEqual(result["profile"]["profile_scores"], {
            "open": 4, "green": 4, "still": 5, "social": 7, "access": 4})

    def test_evidence_reconstructs_every_score_with_explicit_positions(self):
        profile = analyze_saju(prepare_input(request()))["profile"]
        evidence = profile["evidence"]
        self.assertEqual(len({item["id"] for item in evidence}), len(evidence))
        self.assertAlmostEqual(sum(item["weight"] for item in evidence), 1)
        for component in ("natal", "today"):
            self.assertAlmostEqual(sum(item["weight"] for item in evidence if item["component"] == component), .5)
        for axis, detail in profile["score_details"].items():
            self.assertAlmostEqual(sum(item["weight"] * item["vector"][axis] for item in evidence), detail["unrounded"])
        self.assertFalse(any(item["id"] == "saju.natal.day.stem" for item in evidence))
        for item in evidence:
            self.assertTrue({"id", "label", "fact", "rule_id", "vector", "weight"} <= set(item))
            self.assertTrue(item["rule_id"].startswith(POLICY_ID))

    def test_each_branch_has_one_unit_independent_of_hidden_stem_count(self):
        profile = analyze_saju(prepare_input(request()))["profile"]
        for component, key in (("natal", "year"), ("natal", "hour"), ("today", "day")):
            group = [item for item in profile["evidence"] if item["id"].startswith(f"saju.{component}.{key}.hidden.")]
            self.assertTrue(group)
            self.assertAlmostEqual(sum(item["raw_weight"] for item in group), 1)

    def test_mixed_timezone_uses_civil_day_master_for_every_ten_god(self):
        context = prepare_input(request(birth_date="2000-01-01", birth_time="00:30"))
        local = context["birth_local"]
        civil = Solar.fromDate(local).getLunar().getEightChar()
        civil.setSect(2)
        boundary = Solar.fromDate(local.astimezone(timezone(timedelta(hours=8)))).getLunar().getEightChar()
        boundary.setSect(2)
        self.assertEqual(civil.getDay(), "戊午")
        self.assertEqual(boundary.getDay(), "丁巳")
        result = analyze_saju(context)
        self.assertEqual(result["analysis"]["natal"]["day_master"]["text"], "戊")
        for chart_name in ("natal", "today"):
            for pillar in result["analysis"][chart_name]["pillars"].values():
                if pillar["stem_ten_god"]:
                    self.assertEqual(pillar["stem_ten_god"]["chinese"], LunarUtil.SHI_SHEN["戊" + pillar["stem"]])
                for hidden in pillar["hidden_stems"]:
                    self.assertEqual(hidden["ten_god"]["chinese"], LunarUtil.SHI_SHEN["戊" + hidden["text"]])

    def test_twelve_stage_direction_examples(self):
        for stem, branch, expected in (("甲", "亥", "长生"), ("甲", "寅", "临官"),
                                       ("乙", "午", "长生"), ("乙", "卯", "临官"),
                                       ("辛", "子", "长生")):
            with self.subTest(stem=stem, branch=branch):
                self.assertEqual(_stage(stem, branch)["chinese"], expected)
        self.assertIsNone(_stage(None, "子"))

    def test_root_evidence_identifies_exact_positions_without_strength(self):
        pillars = {key: _enrich(_plain_pillar(text), "甲") for key, text in
                   (("year", "甲寅"), ("month", "丙辰"), ("day", "甲子"), ("hour", "壬申"))}
        roots = _root_evidence(pillars, "甲")
        self.assertEqual([(r["pillar"], r["hidden_stem"], r["same_stem"]) for r in roots],
                         [("year", "甲", True), ("month", "乙", False)])
        self.assertTrue(all("strength" not in r and "probability" not in r for r in roots))

    def test_pair_relations_do_not_claim_transformation(self):
        relations = _relations({"year": _plain_pillar("甲子"), "month": _plain_pillar("己丑"),
                                "day": _plain_pillar("庚午"), "hour": None})
        kinds = {(item["kind"], tuple(item["positions"])) for item in relations}
        self.assertIn(("stem_combine", ("year", "month")), kinds)
        self.assertIn(("branch_combine", ("year", "month")), kinds)
        self.assertIn(("branch_clash", ("year", "day")), kinds)
        self.assertTrue(all(item["transformation_assessed"] is False for item in relations))

    def test_unknown_time_omits_hour_and_renormalizes_surviving_evidence(self):
        result = analyze_saju(prepare_input(request(birth_time=None)))
        self.assertIsNone(result["analysis"]["natal"]["pillars"]["hour"])
        self.assertEqual(result["analysis"]["natal"]["available_pillar_count"], 3)
        self.assertTrue(any(item["field"] == "saju.natal.hour" for item in result["omissions"]))
        self.assertFalse(any("natal.hour" in item["id"] for item in result["profile"]["evidence"]))
        self.assertAlmostEqual(sum(item["weight"] for item in result["profile"]["evidence"]), 1)

    def test_unknown_solar_term_boundary_does_not_pick_year_or_month(self):
        result = analyze_saju(prepare_input(request(birth_date="2026-02-04", birth_time=None)))
        natal = result["analysis"]["natal"]
        self.assertIsNone(natal["pillars"]["year"])
        self.assertIsNone(natal["pillars"]["month"])
        self.assertIsNone(natal["month_context"])
        self.assertEqual(len(natal["alternatives"]["pillars"]["month"]), 2)
        self.assertTrue(all("natal.year" not in e["id"] and "natal.month" not in e["id"]
                            for e in result["profile"]["evidence"]))

    def test_ambiguous_day_master_propagates_to_dependent_analysis(self):
        context = prepare_input(request(birth_time=None))
        # Deliberately wider than the current form permits: analysis must still
        # preserve uncertainty instead of choosing an arbitrary reference stem.
        context["birth_samples"] = [datetime(1990, 6, day, 12, tzinfo=ZoneInfo("Asia/Seoul")) for day in (15, 16)]
        result = analyze_saju(context)
        self.assertIsNone(result["analysis"]["natal"]["day_master"])
        self.assertEqual(result["profile"]["status"], "unavailable")
        self.assertIsNone(result["profile"]["profile_scores"])
        self.assertEqual(result["profile"]["evidence"], [])
        for pillar in result["analysis"]["today"]["pillars"].values():
            self.assertIsNone(pillar["stem_ten_god"])
            self.assertIsNone(pillar["twelve_stage"])

    def test_policy_and_highlights_are_inspectable_and_not_old_blend(self):
        result = analyze_saju(prepare_input(request()))
        policy = result["profile"]["policy"]
        self.assertEqual(policy["component_weights"], {"natal": .5, "today": .5})
        self.assertFalse(policy["externally_validated"])
        self.assertEqual(len(result["highlights"]), 6)
        self.assertEqual(len(result["display_sections"]), 2)
        self.assertEqual(len(result["display_sections"][0]["rows"]), 4)
        policy["themes"]["peer"]["vector"]["social"] = 1
        self.assertEqual(policy_definition()["themes"]["peer"]["vector"]["social"], 9)

    def test_exact_half_is_rounded_up_and_retained_for_final_fusion(self):
        natal = {"pillars": {"day": _enrich(_plain_pillar("甲子"), "甲", is_natal_day=True)}}
        today = {"pillars": {"day": _enrich(_plain_pillar("庚申"), "甲")}}
        profile = _make_profile(natal, today, "甲")
        # Natal 子 contains 癸 (resource): green=9. Today's 申 contributes
        # resource mass 1/6: green=4. Their equal mean is exactly 6.5.
        self.assertEqual(profile["score_details"]["green"]["unrounded"], 6.5)
        self.assertEqual(profile["profile_scores"]["green"], 7)

    def test_result_is_deterministic_json_safe_and_does_not_mutate_context(self):
        context = prepare_input(request())
        before = copy.deepcopy(context)
        first = analyze_saju(context)
        self.assertEqual(first, analyze_saju(context))
        self.assertEqual(context, before)
        self.assertTrue(json.dumps(first, allow_nan=False))
        self.assertNotIn("birth_date", first["analysis"]["natal"])
        for value in first["profile"]["profile_scores"].values():
            self.assertTrue(1 <= value <= 10)


if __name__ == "__main__":
    unittest.main()
