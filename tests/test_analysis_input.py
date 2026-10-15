"""Civil-time, birth-location and privacy contract shared by both analyses."""
import builtins
import copy
from datetime import date, datetime, timezone
import io
import contextlib
from pathlib import Path
import unittest
from unittest.mock import patch

from luckypath.analysis_input import AnalysisInputError, prepare_input


BASE = {"birth_date": "1990-05-15", "birth_time": "14:30",
        "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13"}
NOW = datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc)


class AnalysisInputTests(unittest.TestCase):
    def calculate(self, **changes):
        with patch("luckypath.analysis_input.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            result = prepare_input({**BASE, **changes})
            clock.now.assert_called_once_with(timezone.utc)
            return result

    def test_known_input_is_one_real_instant_with_no_fabricated_birth_location(self):
        context = self.calculate()
        self.assertEqual(context["birth_date"], date(1990, 5, 15))
        self.assertEqual(context["birth_timezone"].key, "Asia/Seoul")
        self.assertEqual(context["birth_local"].isoformat(), "1990-05-15T14:30:00+09:00")
        self.assertEqual(context["birth_samples"], [context["birth_local"]])
        self.assertIsNone(context["latitude"])
        self.assertIsNone(context["longitude"])
        self.assertEqual(context["house_system"], "P")
        self.assertTrue(context["birth_window"]["time_known"])
        self.assertEqual(context["birth_window"]["birth_window_hours"], 0)

    def test_coordinate_pairs_allow_zero_and_boundaries_and_house_system_is_explicit(self):
        for latitude, longitude in ((0, 0), (-90, -180), (90, 180), (37.5665, 126.9780)):
            with self.subTest(latitude=latitude, longitude=longitude):
                result = self.calculate(birth_latitude=latitude, birth_longitude=longitude,
                                        house_system="W")
                self.assertEqual(result["latitude"], float(latitude))
                self.assertEqual(result["longitude"], float(longitude))
                self.assertEqual(result["house_system"], "W")
        for value in (None, "p", "K", "", True, 0, [], {}):
            with self.subTest(house_system=value):
                with self.assertRaises(AnalysisInputError):
                    self.calculate(house_system=value)

    def test_coordinate_pair_must_be_complete_numeric_finite_and_in_range(self):
        for partial in ({"birth_latitude": 37}, {"birth_longitude": 127}):
            with self.assertRaisesRegex(AnalysisInputError, "함께"):
                self.calculate(**partial)
        for field, valid, outside in (("birth_latitude", {"birth_longitude": 127}, 90.01),
                                      ("birth_longitude", {"birth_latitude": 37}, 180.01)):
            for value in (None, True, False, "37", float("nan"), float("inf"),
                          -float("inf"), outside, -outside, 10 ** 1000, [], {}):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(AnalysisInputError):
                        self.calculate(**valid, **{field: value})

    def test_dst_overlap_requires_fold_and_resolves_two_different_instants(self):
        inputs = {"birth_date": "2021-11-07", "birth_time": "01:30",
                  "birth_timezone": "America/New_York"}
        with self.assertRaisesRegex(AnalysisInputError, "두 번"):
            self.calculate(**inputs)
        first = self.calculate(**inputs, birth_fold=0)["birth_local"]
        second = self.calculate(**inputs, birth_fold=1)["birth_local"]
        self.assertEqual((second.astimezone(timezone.utc) - first.astimezone(timezone.utc)).total_seconds(), 3600)
        self.assertEqual((first.fold, second.fold), (0, 1))
        with self.assertRaisesRegex(AnalysisInputError, "존재하지 않는 출생 시간"):
            self.calculate(birth_date="2021-03-14", birth_time="02:30",
                           birth_timezone="America/New_York")

    def test_unknown_time_spans_real_23_and_25_hour_days_and_never_chooses_noon(self):
        for birthday, hours in (("2021-03-14", 23), ("2021-11-07", 25)):
            with self.subTest(birthday=birthday):
                result = self.calculate(birth_date=birthday, birth_time=None,
                                        birth_timezone="America/New_York")
                samples = result["birth_samples"]
                self.assertIsNone(result["birth_local"])
                self.assertEqual(result["birth_window"]["civil_day_hours"], hours)
                self.assertEqual(result["birth_window"]["birth_window_hours"], hours)
                self.assertFalse(result["birth_window"]["time_window_clipped_to_now"])
                self.assertGreater(len(samples), 20)
                self.assertEqual(samples[0].strftime("%H:%M:%S"), "00:00:00")
                self.assertEqual(samples[-1].strftime("%H:%M:%S"), "23:59:59")
                self.assertTrue(all(sample.date() == date.fromisoformat(birthday) for sample in samples))
                instants = [sample.astimezone(timezone.utc) for sample in samples]
                self.assertEqual(len(instants), len(set(instants)))

    def test_unknown_time_on_current_birthplace_date_is_clipped_to_one_captured_now(self):
        result = self.calculate(birth_date="2026-10-14", birth_time=None)
        window = result["birth_window"]
        self.assertEqual(window["civil_day_hours"], 24)
        self.assertEqual(window["birth_window_hours"], 7)
        self.assertTrue(window["time_window_clipped_to_now"])
        self.assertEqual(result["birth_samples"][-1].astimezone(timezone.utc), NOW)
        self.assertTrue(all(sample.astimezone(timezone.utc) <= NOW for sample in result["birth_samples"]))
        with self.assertRaisesRegex(AnalysisInputError, "현재보다 이전"):
            self.calculate(birth_date="2026-10-14", birth_time="08:00")
        with self.assertRaisesRegex(AnalysisInputError, "현재보다 이전"):
            self.calculate(birth_date="2026-10-14", birth_time=None,
                           birth_timezone="America/New_York")

    def test_skipped_civil_date_rejected_and_target_is_new_york_noon_in_both_seasons(self):
        with self.assertRaisesRegex(AnalysisInputError, "존재하지 않는 출생 날짜"):
            self.calculate(birth_date="2011-12-30", birth_time=None, birth_timezone="Pacific/Apia")
        for target_date, offset in (("2026-01-13", "-05:00"), ("2026-07-13", "-04:00")):
            result = self.calculate(target_date=target_date)
            self.assertEqual(result["target_local"].isoformat(), target_date + "T12:00:00" + offset)

    def test_invalid_contract_errors_do_not_echo_private_values(self):
        invalid = [None, [], "private-canary", {},
                   {**BASE, "unexpected": "private-canary"},
                   {**BASE, "birth_date": "private-canary"},
                   {**BASE, "birth_date": "1990-02-31"},
                   {**BASE, "birth_timezone": "private-canary"},
                   {**BASE, "birth_time": "private-canary"},
                   {**BASE, "target_timezone": "private-canary"},
                   {**BASE, "birth_fold": True}, {**BASE, "birth_fold": 2},
                   {**BASE, "target_date": "2101-01-01"},
                   {**BASE, "birth_date": "1899-12-31"}]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(AnalysisInputError) as caught:
                    prepare_input(payload)
                self.assertNotIn("private-canary", str(caught.exception))
                self.assertNotIn("/Users/", str(caught.exception))

    def test_no_blended_profile_call_persistence_logging_or_payload_mutation(self):
        original = {**BASE, "birth_latitude": 37.5665, "birth_longitude": 126.9780}
        payload = copy.deepcopy(original)
        real_open = builtins.open

        def read_only_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("Input preparation must not persist a file")
            return real_open(file, mode, *args, **kwargs)

        output = io.StringIO()
        with patch("luckypath.astro_profile.build_profile", side_effect=AssertionError("Do not call old profile")), \
                patch("builtins.open", side_effect=read_only_open), \
                patch.object(Path, "write_text", side_effect=AssertionError("Unexpected persistence")), \
                patch.object(Path, "write_bytes", side_effect=AssertionError("Unexpected persistence")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            context = prepare_input(payload)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(payload, original)
        # Only uncertainty metadata is suitable for the derived public result.
        public_window = str(context["birth_window"])
        for private in (BASE["birth_date"], BASE["birth_time"], BASE["birth_timezone"], "37.5665", "126.978"):
            self.assertNotIn(private, public_window)


if __name__ == "__main__":
    unittest.main()
