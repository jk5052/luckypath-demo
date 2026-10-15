"""External information stays optional and behind the local HTTP boundary.

These tests use the shipped Williamsburg network and a recording provider.
They never call an external API or require a credential.
"""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from luckypath.engine import Engine
from luckypath.explore import ExploreService
from luckypath.external_services import ExternalServices
from luckypath.server import make_server


PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT.parent / "luckypath-nyc-data" / "quality" / "pilot" / "williamsburg"
EXTERNAL_PATHS = {
    "/explore/search": "search",
    "/explore/weather": "weather",
    "/explore/reference": "reference",
    "/explore/external/settings": "set_ors_key",
}


class RecordingExternalServices:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.failure = None

    def config(self):
        return {"ors": {"configured": False}, "automatic_requests": False}

    def _invoke(self, method, request):
        self.calls.append((method, copy.deepcopy(request)))
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.responses.get(method, {
            "status": "unavailable", "reason": "test_provider_unavailable",
        }))

    def search(self, request):
        return self._invoke("search", request)

    def weather(self, request):
        return self._invoke("weather", request)

    def reference(self, request):
        return self._invoke("reference", request)

    def set_ors_key(self, request):
        return self._invoke("set_ors_key", request)


@unittest.skipUnless((DATA / "dataset.json").is_file(), "Requires prepared NYC Williamsburg data")
class ExternalHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(DATA)
        cls.provider = RecordingExternalServices()
        cls.explore = ExploreService(cls.engine, DATA, external_services=cls.provider)
        cls.route_request = {
            **copy.deepcopy(cls.explore.initial_request),
            "time_budget": {"max_minutes": 30},
        }
        cls.baseline = cls.explore.recommend(cls.route_request)
        cls.allowed_origin = "http://localhost:5173"
        cls.server = make_server(cls.engine, port=0, explore=cls.explore,
                                 allowed_origin=cls.allowed_origin)
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
            raise RuntimeError("External HTTP test server did not stop")

    def setUp(self):
        self.provider.calls.clear()
        self.provider.responses.clear()
        self.provider.failure = None

    def http(self, method, path, payload=None, *, headers=None, body=None, port=None):
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            headers = dict(response.getheaders())
            result = json.loads(raw) if "application/json" in headers.get("Content-Type", "") else raw
            return response.status, headers, result
        finally:
            connection.close()

    def payload_for(self, path):
        if path == "/explore/search":
            return {"query": "park"}
        if path == "/explore/reference":
            return {key: self.route_request[key] for key in ("origin", "destination", "time_budget")}
        if path == "/explore/weather":
            return {"duration_minutes": 30}
        return {"ors_api_key": ""}

    def test_local_config_context_and_budget_route_never_trigger_external_requests(self):
        for path in ("/explore/config", "/explore/context"):
            status, headers, result = self.http("GET", path)
            self.assertEqual(status, 200, result)
            self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, config = self.http("GET", "/explore/config")
        self.assertEqual(config["external_services"], self.provider.config())
        self.assertFalse(config["capabilities"]["local_route_requires_external_api"])
        self.assertFalse(config["capabilities"]["external_reference_is_recommendation_eta"])
        status, _, result = self.http("POST", "/explore/route", self.route_request)
        self.assertEqual(status, 200, result)
        self.assertEqual(result, self.baseline)
        self.assertEqual(self.provider.calls, [])

    def test_external_endpoints_dispatch_only_explicit_same_origin_json_requests(self):
        for path, method in EXTERNAL_PATHS.items():
            with self.subTest(path=path):
                expected = {"status": "ok", "provider": "fixture", "method": method}
                self.provider.responses[method] = expected
                payload = self.payload_for(path)
                status, headers, result = self.http("POST", path, payload,
                    headers={"Origin": f"http://127.0.0.1:{self.port}"})
                self.assertEqual(status, 200, result)
                self.assertEqual(result, expected)
                self.assertEqual(self.provider.calls[-1], (method, payload))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(self.http("GET", path)[0], 404)

    def test_external_access_rejects_cross_origin_and_nonlocal_hosts_before_dispatch(self):
        for path in EXTERNAL_PATHS:
            for headers in (
                {"Origin": "https://example.com"},
                {"Origin": self.allowed_origin},
                {"Origin": "null"},
                {"Host": "private.example"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Origin": f"http://127.0.0.1:{self.port + 1}"},
            ):
                with self.subTest(path=path, headers=headers):
                    status, _, result = self.http("POST", path, self.payload_for(path), headers=headers)
                    self.assertEqual(status, 403, result)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.http("GET", "/explore/config",
                                   headers={"Host": "private.example"})[0], 403)
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(self.engine, host="0.0.0.0", port=0, explore=self.explore)

    def test_mime_body_limits_and_malformed_json_never_reach_external_provider(self):
        for path in EXTERNAL_PATHS:
            for body, headers, expected in (
                (b"{}", {"Content-Type": "text/plain"}, 415),
                (b"{}", {"Content-Type": "application/json", "Content-Length": "65537"}, 413),
                (b"{", {"Content-Type": "application/json"}, 400),
                (b"", {"Content-Type": "application/json"}, 413),
            ):
                with self.subTest(path=path, expected=expected):
                    self.assertEqual(self.http("POST", path, body=body, headers=headers)[0], expected)
        self.assertEqual(self.provider.calls, [])

    def test_saved_place_name_search_falls_back_when_remote_provider_is_unavailable(self):
        name = self.explore._places[0]["name"]
        status, _, result = self.http("POST", "/explore/search", {"query": name})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["provider"], "local")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["reason"], "test_provider_unavailable")
        self.assertTrue(any(row["name"] == name for row in result["results"]))
        self.assertLessEqual(len(result["results"]), 6)
        self.assertTrue(all(self.explore._inside(row["lonlat"]) for row in result["results"]))
        self.assertEqual(self.provider.calls, [("search", {"query": name})])

    def test_search_rejects_invalid_and_extra_private_fields_before_dispatch(self):
        for payload in ([], {}, {"query": 15}, {"query": "a"}, {"query": "  "},
                        {"query": "a" * 161}, {"query": "park\u0001"},
                        {"query": "park", "birth_date": "private-birth-canary"}):
            with self.subTest(payload=payload):
                status, _, result = self.http("POST", "/explore/search", payload)
                self.assertEqual(status, 400, result)
                self.assertNotIn("private-birth-canary", json.dumps(result))
        self.assertEqual(self.provider.calls, [])

    def test_reference_rejects_points_outside_the_active_map_before_dispatch(self):
        valid = self.payload_for("/explore/reference")
        for field in ("origin", "destination", "via"):
            with self.subTest(field=field):
                status, _, result = self.http("POST", "/explore/reference",
                                               {**valid, field: [-74.0, 40.7]})
                self.assertEqual(status, 400, result)
                self.assertIn("study area", result["error"])
        self.assertEqual(self.provider.calls, [])

    def test_external_failure_is_sanitized_and_does_not_change_local_budgeted_route(self):
        self.provider.failure = RuntimeError("private-key-canary /Users/private-provider-path")
        for path in EXTERNAL_PATHS:
            with self.subTest(path=path):
                status, headers, result = self.http("POST", path, self.payload_for(path))
                self.assertEqual(status, 500, result)
                self.assertEqual(result, {"error": "External information could not be retrieved"})
                self.assertEqual(headers["Cache-Control"], "no-store")
        before = copy.deepcopy(self.provider.calls)
        status, _, result = self.http("POST", "/explore/route", self.route_request)
        self.assertEqual(status, 200, result)
        self.assertEqual(result, self.baseline)
        self.assertEqual(self.provider.calls, before)

    def test_external_request_paths_do_not_enter_access_logs_even_on_rejection(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            for path in EXTERNAL_PATHS:
                private_path = path + "?private-query-canary"
                self.assertEqual(self.http("POST", private_path, self.payload_for(path))[0], 200)
                self.assertEqual(self.http("GET", private_path)[0], 404)
                self.assertEqual(self.http("POST", private_path, self.payload_for(path),
                                           headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(output.getvalue(), "")

    def test_external_endpoints_are_not_exposed_without_the_explorer(self):
        server = make_server(self.engine, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for path in EXTERNAL_PATHS:
                self.assertEqual(self.http("POST", path, self.payload_for(path), port=port)[0], 404)
            _, _, health = self.http("GET", "/health", port=port)
            self.assertFalse(any("/explore/" in endpoint for endpoint in health["endpoints"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_external_browser_module_is_allowlisted_without_exposing_python_or_paths(self):
        status, headers, body = self.http("GET", "/explore/assets/external.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertTrue(body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        for path in ("/explore/assets/../external_services.py",
                     "/explore/assets/../../server.py",
                     "/explore/assets/external_services.py"):
            self.assertEqual(self.http("GET", path)[0], 404)

    def test_real_provider_contract_rejects_birth_fields_and_never_returns_session_key(self):
        network_calls = []
        def no_network(*args, **kwargs):
            network_calls.append((args, kwargs))
            raise AssertionError("No external request is permitted in this contract test")
        with patch.dict(os.environ, {}, clear=True):
            actual_provider = ExternalServices(self.explore.bbox, opener=no_network)
        self.explore.external = actual_provider
        output = io.StringIO()
        try:
            with contextlib.redirect_stderr(output):
                for path in EXTERNAL_PATHS:
                    status, headers, result = self.http("POST", path, {
                        **self.payload_for(path), "birth_date": "private-birth-canary",
                    })
                    self.assertEqual(status, 400, (path, result))
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertNotIn("private-birth-canary", json.dumps(result))
                status, headers, result = self.http("POST", "/explore/external/settings",
                    {"ors_api_key": "private-session-key-canary"})
                self.assertEqual(status, 200, result)
                self.assertTrue(result["ors"]["configured"])
                self.assertEqual(result["ors"]["key_source"], "session")
                self.assertFalse(result["ors"]["key_validated"])
                self.assertFalse(result["ors"]["session_key_persisted"])
                self.assertNotIn("private-session-key-canary", json.dumps(result))
            status, _, config = self.http("GET", "/explore/config")
            self.assertEqual(status, 200, config)
            self.assertTrue(config["external_services"]["ors"]["configured"])
            self.assertNotIn("private-session-key-canary", json.dumps(config))
            self.assertEqual(network_calls, [])
            self.assertEqual(output.getvalue(), "")
        finally:
            self.explore.external = self.provider


if __name__ == "__main__":
    unittest.main()
