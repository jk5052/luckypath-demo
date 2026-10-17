"""Local profile HTTP boundary, privacy and route/profile separation.

Uses the real ExploreService.profile/build_independent_profile, without NYC downloads or
human birth data. The lightweight engine handles only early route validation;
the existing route integration tests cover actual geometry calculation.
"""
import builtins
import contextlib
from datetime import date
import http.client
import io
import json
from pathlib import Path
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from luckypath.engine import Engine
from luckypath.explore import ExploreService
from luckypath.server import make_server


PROFILE_INPUT = {
    "birth_date": "1990-05-15", "birth_time": "14:30",
    "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13",
}
ROUTE_INPUT = {
    "origin": [-73.9577, 40.7178], "destination": [-73.9670, 40.7147],
    "profile_scores": dict.fromkeys(("open", "green", "still", "social", "access"), 5),
    "lucky_limit": 0,
}


class AstroHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = object.__new__(Engine)
        cls.engine.normalization = {"id": "fixture"}
        cls.engine.evidence = SimpleNamespace(metadata=lambda: {"empirical_quality_verified": False})
        cls.explore = object.__new__(ExploreService)
        cls.explore.engine = cls.engine
        cls.explore.bbox = [-73.970, 40.709, -73.951, 40.724]
        cls.explore.external = SimpleNamespace(config=lambda: {"ors": {"configured": False}, "weather": {"enabled": True}})
        cls.explore.initial_request = ROUTE_INPUT
        cls.explore._places = []
        cls.server = make_server(cls.engine, port=0, explore=cls.explore)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": .01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        if cls.thread.is_alive():
            raise RuntimeError("Profile HTTP test server did not stop")

    def http(self, method, path="/explore/profile", payload=None, *, headers=None, body=None):
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_profile_dispatches_independent_analyses_and_returns_one_final_slider_profile(self):
        status, headers, payload = self.http("POST", payload=PROFILE_INPUT)
        self.assertEqual(status, 200, payload)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["profile_version"], "route-maker-reading-v1")
        self.assertEqual(payload["profile_status"], "ready", payload)
        self.assertEqual(set(payload["systems"]), {"saju", "astrology"})
        for legacy in ("profile_scores", "natal", "today", "component_details"):
            self.assertNotIn(legacy, payload)
        for profile in (payload["systems"]["saju"]["profile"],
                        payload["systems"]["astrology"]["profile"], payload["combined"]):
            self.assertEqual(profile["status"], "ready")
            self.assertEqual(set(profile["profile_scores"]), set(ROUTE_INPUT["profile_scores"]))
            for value in profile["profile_scores"].values():
                self.assertIsInstance(value, (int, float))
                self.assertNotIsInstance(value, bool)
                self.assertGreaterEqual(value, 1)
                self.assertLessEqual(value, 10)
        self.assertEqual(payload["combined"]["policy"]["stage"], "after_independent_event_quality_before_street_matrix")
        self.assertEqual(payload["combined"]["routing_weights"], payload["route_maker_reading"]["routing_weights"])
        json.dumps(payload, allow_nan=False)

    def test_config_exposes_calendar_current_target_date_and_unvalidated_local_policy(self):
        status, _, payload = self.http("GET", "/explore/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["capabilities"]["astrology_interpretation_connected"])
        config = payload["profile"]
        self.assertEqual(config["schema_version"], 2)
        self.assertEqual(config["version"], "route-maker-reading-v1")
        self.assertEqual(config["analysis_systems"], ["saju", "astrology"])
        self.assertEqual(config["calendar"], "gregorian")
        self.assertEqual(config["target_timezone"], "America/New_York")
        date.fromisoformat(config["default_target_date"])
        date.fromisoformat(config["birth_date_max"])
        self.assertFalse(config["persists_birth_data"])
        self.assertFalse(config["externally_validated"])

    def test_invalid_birth_payloads_return_client_errors_without_raw_values_or_paths(self):
        invalid = [
            {}, {**PROFILE_INPUT, "birth_date": "1990-02-31"},
            {**PROFILE_INPUT, "birth_time": "24:99"},
            {**PROFILE_INPUT, "birth_timezone": "private-birth-zone-canary"},
            {**PROFILE_INPUT, "unexpected_private_note": "private-canary-do-not-log"},
            {**PROFILE_INPUT, "birth_date": True},
            {**PROFILE_INPUT, "birth_fold": 2},
            {**PROFILE_INPUT, "birth_latitude": 37.5},
            {**PROFILE_INPUT, "birth_latitude": 37.5, "birth_longitude": True},
            {**PROFILE_INPUT, "birth_latitude": 91, "birth_longitude": 127},
            {**PROFILE_INPUT, "house_system": "private-canary-do-not-log"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                status, _, result = self.http("POST", payload=payload)
                self.assertEqual(status, 400, result)
                self.assertIn("error", result)
                self.assertNotIn("Traceback", result["error"])
                self.assertNotIn("/Users/", result["error"])
                self.assertNotIn("private-birth-zone-canary", result["error"])
                self.assertNotIn("private-canary-do-not-log", result["error"])

    def test_unknown_birth_time_is_valid_and_does_not_guess_a_required_hour(self):
        status, _, payload = self.http("POST", payload={**PROFILE_INPUT, "birth_time": None})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["profile_status"], "ready", payload)
        self.assertTrue(any(system.get("omissions") for system in payload["systems"].values()))
        self.assertEqual(set(payload["combined"]["profile_scores"]), set(ROUTE_INPUT["profile_scores"]))

    def test_missing_provider_never_falls_back_to_one_system_or_the_old_blended_policy(self):
        from luckypath.analysis_input import prepare_input
        from luckypath.saju_analysis import analyze_saju
        ready = analyze_saju(prepare_input(PROFILE_INPUT))
        def missing(_context):
            raise RuntimeError("private-provider-path /Users/private-canary")
        modules = {
            "luckypath.saju_analysis": SimpleNamespace(analyze_saju=lambda _context: ready),
            "luckypath.astrology_analysis": SimpleNamespace(analyze_astrology=missing),
        }
        with patch.dict(sys.modules, modules), \
                patch("luckypath.astro_profile.build_profile", side_effect=AssertionError("Legacy fallback forbidden")):
            status, _, payload = self.http("POST", payload=PROFILE_INPUT)
        self.assertEqual(status, 200)
        self.assertEqual(payload["profile_status"], "unavailable")
        self.assertIsNone(payload["combined"])
        self.assertEqual(payload["systems"]["saju"]["profile"]["status"], "ready")
        self.assertEqual(payload["systems"]["astrology"]["profile"]["status"], "unavailable")
        self.assertNotIn("private-canary", json.dumps(payload))
        self.assertNotIn("profile_scores", payload)

    def test_profile_requires_local_same_origin_even_with_valid_json(self):
        headers_to_reject = [
            {"Origin": "https://example.com"}, {"Origin": "null"},
            {"Host": "private.example"}, {"Sec-Fetch-Site": "cross-site"},
            {"Origin": f"http://127.0.0.1:{self.port + 1}"},
        ]
        for headers in headers_to_reject:
            with self.subTest(headers=headers):
                status, _, _ = self.http("POST", payload=PROFILE_INPUT, headers=headers)
                self.assertEqual(status, 403)
        status, _, _ = self.http("POST", payload=PROFILE_INPUT,
                                 headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_wrong_mime_oversized_and_malformed_json_are_rejected(self):
        for body, headers, expected in [
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"{}", {"Content-Type": "application/json", "Content-Length": "65537"}, 413),
            (b'{"birth_date":', {"Content-Type": "application/json"}, 400),
            (b"[]", {"Content-Type": "application/json"}, 400),
        ]:
            with self.subTest(expected=expected, body=body):
                status, _, _ = self.http("POST", body=body, headers=headers)
                self.assertEqual(status, expected)

    def test_profile_requests_never_enter_access_logs_including_query_or_error_paths(self):
        output = io.StringIO()
        path = "/explore/profile?birth_date=private-query-canary"
        with contextlib.redirect_stderr(output):
            self.assertEqual(self.http("POST", path, PROFILE_INPUT)[0], 200)
            self.assertEqual(self.http("POST", path, {})[0], 400)
            self.assertEqual(self.http("GET", path)[0], 404)
            self.assertEqual(self.http("OPTIONS", path)[0], 200)
            self.assertEqual(self.http("POST", path, PROFILE_INPUT,
                                       headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(output.getvalue(), "")

    def test_profile_calculation_does_not_write_files_or_retain_input_in_service(self):
        engine_state = vars(self.engine).copy()
        service_state = vars(self.explore).copy()
        real_open = builtins.open

        def read_only_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("Profile computation must not write a file")
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=read_only_open), \
                patch.object(Path, "write_text", side_effect=AssertionError("Unexpected profile persistence")), \
                patch.object(Path, "write_bytes", side_effect=AssertionError("Unexpected profile persistence")):
            status, _, payload = self.http("POST", payload=PROFILE_INPUT)
        self.assertEqual(status, 200, payload)
        self.assertEqual(vars(self.engine), engine_state)
        self.assertEqual(vars(self.explore), service_state)

    def test_route_boundary_rejects_raw_birth_fields_and_profile_result_objects(self):
        # The route API accepts numeric preferences, never the birth record. This uses
        # Engine.recommend's real unknown-field check before any geometry work.
        for extra in ({"birth_date": PROFILE_INPUT["birth_date"]},
                      {"birth_time": PROFILE_INPUT["birth_time"]},
                      {"birth_timezone": PROFILE_INPUT["birth_timezone"]},
                      {"birth_latitude": 37.5665},
                      {"birth_longitude": 126.9780},
                      {"systems": {"private": "private-canary-do-not-log"}},
                      {"natal": {"private": "private-canary-do-not-log"}}):
            with self.subTest(extra=extra):
                status, _, payload = self.http("POST", "/explore/route", {**ROUTE_INPUT, **extra})
                self.assertEqual(status, 400, payload)
                serialized = json.dumps(payload)
                for raw in (*PROFILE_INPUT.values(), "private-canary-do-not-log"):
                    self.assertNotIn(raw, serialized)

    def test_profile_endpoint_is_opt_in_and_rejects_nonloopback_binding(self):
        with self.assertRaises(ValueError):
            make_server(self.engine, host="0.0.0.0", port=0, explore=self.explore)
        other = make_server(self.engine, port=0)
        worker = threading.Thread(target=other.handle_request, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection("127.0.0.1", other.server_address[1], timeout=10)
        try:
            connection.request("POST", "/explore/profile", body=json.dumps(PROFILE_INPUT),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
            worker.join(timeout=2)
            other.server_close()


if __name__ == "__main__":
    unittest.main()
