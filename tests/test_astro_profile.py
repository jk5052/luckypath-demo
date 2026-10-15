"""Calendar boundaries, actual astronomical longitudes, and uncertainty behavior."""
import json
import math
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from luckypath.astro_profile import AstroProfileError, build_profile


BASE = {
    "birth_date": "2005-12-23", "birth_time": "08:37",
    "birth_timezone": "Asia/Shanghai", "target_date": "2026-10-13",
}


def profile(**changes):
    return build_profile({**BASE, **changes})


class AstroProfileTests(unittest.TestCase):
    def test_lunar_python_readme_example_and_public_contract(self):
        # The upstream README's sample is 2005-12-23 08:37, Beijing time.
        result = profile()
        self.assertEqual([result["natal"]["pillars"][key]["text"]
                          for key in ("year", "month", "day", "hour")],
                         ["乙酉", "戊子", "辛巳", "壬辰"])
        self.assertEqual(result["natal"]["pillars"]["year"]["stem_element"], "wood")
        self.assertEqual(result["natal"]["pillars"]["year"]["branch_element"], "metal")
        self.assertEqual(result["profile_version"], "saju-zodiac-symbolic-v1")
        self.assertEqual(result["profile_version"], result["policy"]["id"])
        self.assertFalse(result["policy"]["externally_validated"])
        self.assertEqual(set(result["profile_scores"]), {"open", "green", "still", "social", "access"})
        self.assertTrue(all(math.isfinite(value) and 1 <= value <= 10
                            for value in result["profile_scores"].values()))
        self.assertEqual(result["conventions"]["libraries"],
                         {"lunar-python": "1.4.8", "astronomy-engine": "2.1.19"})
        self.assertTrue(result["conventions"]["mixed_calendar_convention"])
        self.assertEqual(result, profile())
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        for private_input in ("2005-12-23", "08:37", "Asia/Shanghai"):
            self.assertNotIn(private_input, serialized)

    def test_actual_geocentric_sun_and_moon_longitudes_at_j2000(self):
        natal = profile(birth_date="2000-01-01", birth_time="12:00",
                        birth_timezone="UTC")["natal"]
        # Fixed Astronomy Engine 2.1.19 values, UTC noon. These are geocentric,
        # rather than the unrelated heliocentric EclipticLongitude API.
        self.assertAlmostEqual(natal["sun"]["longitude_deg"], 280.36864016087605, places=7)
        self.assertAlmostEqual(natal["moon"]["longitude_deg"], 223.32389112747416, places=7)
        self.assertEqual(natal["sun"]["sign_key"], "capricorn")
        self.assertEqual(natal["moon"]["sign_key"], "scorpio")

    def test_lichun_changes_year_and_month_at_same_instant_across_zones(self):
        # lunar-python 2024 立春 is Feb 4 16:27:07 fixed UTC+8.
        before = profile(birth_date="2024-02-04", birth_time="17:26",
                         birth_timezone="Asia/Seoul")["natal"]
        after = profile(birth_date="2024-02-04", birth_time="17:28",
                        birth_timezone="Asia/Seoul")["natal"]
        equivalent = profile(birth_date="2024-02-04", birth_time="08:28",
                             birth_timezone="UTC")["natal"]
        self.assertEqual(before["pillars"]["year"]["text"], "癸卯")
        self.assertEqual(after["pillars"]["year"]["text"], "甲辰")
        self.assertEqual(before["pillars"]["month"]["text"], "乙丑")
        self.assertEqual(after["pillars"]["month"]["text"], "丙寅")
        self.assertEqual(before["pillars"]["day"], after["pillars"]["day"])
        for key in ("year", "month"):
            self.assertEqual(after["pillars"][key], equivalent["pillars"][key])
        for key in ("sun", "moon"):
            self.assertEqual(after[key], equivalent[key])

    def test_sect_two_local_day_changes_at_midnight_not_at_23(self):
        earlier = profile(birth_date="2005-12-23", birth_time="22:59")["natal"]
        late = profile(birth_date="2005-12-23", birth_time="23:30")["natal"]
        next_day = profile(birth_date="2005-12-24", birth_time="00:00")["natal"]
        self.assertEqual(earlier["pillars"]["day"]["text"], "辛巳")
        self.assertEqual(late["pillars"]["day"], earlier["pillars"]["day"])
        self.assertEqual(next_day["pillars"]["day"]["text"], "壬午")
        # The hour stem follows the library's separate late-Zi convention.
        self.assertEqual(late["pillars"]["hour"], next_day["pillars"]["hour"])

    def test_unknown_time_keeps_stable_values_without_fake_noon(self):
        result = profile(birth_time=None)
        natal = result["natal"]
        self.assertFalse(natal["time_known"])
        self.assertIsNone(natal["pillars"]["hour"])
        self.assertEqual(natal["alternatives"]["pillars"]["hour"], [])
        self.assertEqual(natal["sun"]["sign_key"], "capricorn")
        self.assertIsNone(natal["sun"]["longitude_deg"])
        self.assertEqual(natal["civil_day_hours"], 24)
        self.assertLess(natal["longitude_ranges_deg"]["sun"]["start"],
                        natal["longitude_ranges_deg"]["sun"]["end"])
        omissions = [item for item in result["omissions"] if item["field"] == "natal.hour"]
        self.assertEqual(len(omissions), 1)
        self.assertIn("출생 시간을 몰라", omissions[0]["reason"])
        self.assertEqual(result["component_details"]["natal_saju"]["signal_count"], 6)
        payload = dict(BASE)
        del payload["birth_time"]
        self.assertEqual(build_profile(payload), result)

    def test_unknown_time_omits_changing_solar_term_pillars_and_moon(self):
        result = profile(birth_date="2024-02-04", birth_time=None,
                         birth_timezone="Asia/Seoul")
        natal = result["natal"]
        for key in ("year", "month"):
            self.assertIsNone(natal["pillars"][key])
            self.assertEqual(len(natal["alternatives"]["pillars"][key]), 2)
        self.assertEqual(natal["pillars"]["day"]["text"], "戊戌")
        self.assertIsNone(natal["moon"])
        self.assertEqual({item["sign_key"] for item in natal["alternatives"]["moon_signs"]},
                         {"scorpio", "sagittarius"})
        fields = [item["field"] for item in result["omissions"]]
        self.assertEqual(len(fields), len(set(fields)))
        self.assertTrue({"natal.year", "natal.month", "natal.hour", "natal.moon"} <= set(fields))

    def test_unknown_equinox_day_exposes_both_signs_across_zero_longitude(self):
        natal = profile(birth_date="2000-03-20", birth_time=None,
                        birth_timezone="America/New_York")["natal"]
        self.assertIsNone(natal["sun"])
        self.assertEqual([item["sign_key"] for item in natal["alternatives"]["sun_signs"]],
                         ["pisces", "aries"])
        self.assertTrue(natal["longitude_ranges_deg"]["sun"]["crosses_zero"])

    def test_known_dst_overlap_requires_fold_and_uses_two_real_instants(self):
        inputs = {"birth_date": "2021-11-07", "birth_time": "01:30",
                  "birth_timezone": "America/New_York"}
        with self.assertRaisesRegex(AstroProfileError, "두 번"):
            profile(**inputs)
        first = profile(**inputs, birth_fold=0)["natal"]
        second = profile(**inputs, birth_fold=1)["natal"]
        self.assertEqual(first["pillars"], second["pillars"])
        self.assertGreater(second["moon"]["longitude_deg"] - first["moon"]["longitude_deg"], 0.4)
        self.assertGreater(second["sun"]["longitude_deg"] - first["sun"]["longitude_deg"], 0.03)

    def test_dst_gap_and_skipped_civil_date_are_rejected(self):
        with self.assertRaisesRegex(AstroProfileError, "존재하지 않는 출생 시간"):
            profile(birth_date="2021-03-14", birth_time="02:30", birth_timezone="America/New_York")
        with self.assertRaisesRegex(AstroProfileError, "존재하지 않는 출생 날짜"):
            profile(birth_date="2011-12-30", birth_time=None, birth_timezone="Pacific/Apia")

    def test_unknown_day_includes_full_23_or_25_hour_day(self):
        for day, expected in (("2021-03-14", 23), ("2021-11-07", 25)):
            with self.subTest(day=day):
                natal = profile(birth_date=day, birth_time=None,
                                birth_timezone="America/New_York")["natal"]
                self.assertEqual(natal["civil_day_hours"], expected)
                self.assertIsNone(natal["pillars"]["hour"])

    def test_daily_reference_is_new_york_noon_in_winter_and_summer(self):
        for target, offset in (("2026-01-13", "-05:00"), ("2026-07-13", "-04:00")):
            with self.subTest(target=target):
                today = profile(target_date=target)["today"]
                self.assertEqual(today["reference_local"], target + "T12:00:00" + offset)
                self.assertEqual(today["timezone"], "America/New_York")
                self.assertEqual(today["date"], target)
        self.assertEqual(profile(), profile(target_timezone="America/New_York"))

    def test_current_day_uses_birthplace_date_and_only_elapsed_hours(self):
        now = datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc)
        with patch("luckypath.astro_profile.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            # It is already Oct 14 in Seoul, while still Oct 13 in New York.
            known = profile(birth_date="2026-10-14", birth_time="06:00",
                            birth_timezone="Asia/Seoul")
            self.assertTrue(known["natal"]["time_known"])
            unknown = profile(birth_date="2026-10-14", birth_time=None,
                              birth_timezone="Asia/Seoul")["natal"]
            self.assertEqual(unknown["civil_day_hours"], 24)
            self.assertAlmostEqual(unknown["birth_window_hours"], 7, delta=1 / 3600)
            self.assertTrue(unknown["time_window_clipped_to_now"])
            self.assertIn("현재 순간", unknown["time_window_note"])
            with self.assertRaisesRegex(AstroProfileError, "현재보다 이전"):
                profile(birth_date="2026-10-14", birth_time="08:00",
                        birth_timezone="Asia/Seoul")
            with self.assertRaisesRegex(AstroProfileError, "현재보다 이전"):
                profile(birth_date="2026-10-14", birth_time=None,
                        birth_timezone="America/New_York")

    def test_validation_is_strict_and_does_not_echo_private_values(self):
        invalid = [
            None, [], "private-person", {},
            {**BASE, "private-extra-field": "private-person"},
            {**BASE, "birth_date": "private-person"},
            {**BASE, "birth_date": "2001-02-29"},
            {**BASE, "birth_date": "1899-12-31"},
            {**BASE, "birth_date": "2100-01-01"},
            {**BASE, "birth_time": "24:00"},
            {**BASE, "birth_time": "8:37"},
            {**BASE, "birth_time": 837},
            {**BASE, "birth_timezone": "private-person"},
            {**BASE, "birth_timezone": "../../private-person"},
            {**BASE, "birth_fold": True},
            {**BASE, "birth_fold": 2},
            {**BASE, "birth_fold": float("nan")},
            {**BASE, "target_date": "2101-01-01"},
            {**BASE, "target_date": "1899-01-01"},
            {**BASE, "target_date": "2026-02-30"},
            {**BASE, "target_timezone": "Asia/Seoul"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(AstroProfileError) as error:
                    build_profile(payload)
                message = str(error.exception)
                self.assertNotIn("private-person", message)
                self.assertNotIn("private-extra-field", message)
                self.assertTrue(any("가" <= character <= "힣" for character in message))


if __name__ == "__main__":
    unittest.main()
