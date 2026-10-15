"""Provider boundary tests use synthetic responses, never real keys or API quota."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from luckypath.external_services import (
    ENV_KEYS, ExternalServices, MAX_CACHE_ENTRIES, MAX_RESPONSE_BYTES,
    TIMEOUT_SECONDS, WEATHER_UNITS, _NoRedirect, _ProviderFailure,
)


BBOX = [-73.970, 40.709, -73.951, 40.724]
NOW = datetime(2026, 10, 13, 10, 45, tzinfo=timezone.utc)
SYNTHETIC_KEY = "synthetic-unit-test-token"


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, data, url):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.url = url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, data=None, error=None, redirected_url=None):
        self.data = data
        self.error = error
        self.redirected_url = redirected_url
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.redirected_url or request.full_url)


def point(name="Williamsburg Library", lon=-73.96, lat=40.715):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"label": name, "untrusted_extra": "not copied"}}


def forecast(now=NOW):
    first = now.replace(minute=0, second=0, microsecond=0)
    stamps = [int((first + timedelta(hours=i)).timestamp()) for i in range(-1, 7)]
    return {"utc_offset_seconds": 0, "hourly_units": dict(WEATHER_UNITS), "hourly": {
        "time": stamps, "temperature_2m": [19, 20, 21, 22, 23, 24, 25, 26],
        "apparent_temperature": [18, 19, 20, 21, 22, 23, 24, 25],
        "precipitation_probability": [90, 90, 10, 20, 70, 20, 10, 0],
        "precipitation": [100, 100, 1, 2, 3, 4, 5, 6],
        "wind_speed_10m": [5, 10, 11, 12, 13, 14, 15, 16]}}


def route_response():
    return {"features": [{"type": "Feature", "geometry": {"type": "LineString",
                            "coordinates": [[-73.969, 40.712], [-73.955, 40.719]]},
                          "properties": {"summary": {"distance": 1200, "duration": 900},
                                         "untrusted_extra": SYNTHETIC_KEY}}],
            "metadata": {"query": {"secret": SYNTHETIC_KEY}}}


def route_request():
    return {"origin": [-73.969, 40.712], "destination": [-73.955, 40.719],
            "time_budget": {"max_minutes": 30, "walking_speed_kmh": 3.5,
                            "buffer_minutes": 3, "stop_minutes": 0}}


class ExternalServiceTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {name: "" for name in ENV_KEYS})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def service(self, data=None, *, key=False, opener=None, now=None):
        transport = opener or FakeOpener(data)
        service = ExternalServices(BBOX, opener=transport, now=now or (lambda: NOW))
        if key:
            service.set_ors_key({"ors_api_key": SYNTHETIC_KEY})
        return service, transport

    def test_no_key_is_explicit_and_never_calls_provider(self):
        service, transport = self.service()
        self.assertEqual(service.config()["ors"]["key_source"], "none")
        for result in (service.search({"query": "Bedford Ave"}), service.reference(route_request())):
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "missing_key")
        self.assertEqual(transport.calls, [])
        self.assertTrue(service.config()["weather"]["enabled"])

    def test_environment_precedence_session_override_and_clear(self):
        with patch.dict(os.environ, dict(zip(ENV_KEYS, ["first-env", "second-env", "third-env"]))):
            service, transport = self.service({"features": []})
        self.assertEqual(service.config()["ors"]["key_source"], "environment")
        service.search({"query": "Bedford"})
        self.assertEqual(transport.calls[-1][0].get_header("Authorization"), "first-env")
        settings = service.set_ors_key({"ors_api_key": SYNTHETIC_KEY})
        self.assertEqual(settings["ors"]["key_source"], "session")
        self.assertFalse(settings["ors"]["key_validated"])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(settings))
        service.search({"query": "Bedford"})
        self.assertEqual(transport.calls[-1][0].get_header("Authorization"), SYNTHETIC_KEY)
        service.set_ors_key({"ors_api_key": ""})
        self.assertEqual(service.config()["ors"]["key_source"], "environment")
        service.search({"query": "Bedford"})
        self.assertEqual(transport.calls[-1][0].get_header("Authorization"), "first-env")

    def test_key_rejects_headers_extra_fields_or_non_string_without_echo(self):
        service, _ = self.service()
        for value in (None, True, 12, {}, [], "x\r\nAuthorization: secret", "x y", "é", "x" * 513):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as caught:
                service.set_ors_key({"ors_api_key": value})
            self.assertNotIn("secret", str(caught.exception))
        for payload in ({}, {"ors_api_key": "ok", "birth_date": "1990-05-15"}):
            with self.assertRaises(ValueError):
                service.set_ors_key(payload)

    def test_search_sends_bbox_and_header_and_normalizes_trusted_fields(self):
        service, transport = self.service({"features": [point()]}, key=True)
        result = service.search({"query": "  Bedford & N 7th  "})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["lonlat"], [-73.96, 40.715])
        self.assertEqual(set(result["results"][0]), {"id", "name", "lonlat"})
        request, timeout = transport.calls[0]
        self.assertEqual(timeout, TIMEOUT_SECONDS)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), SYNTHETIC_KEY)
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(urlsplit(request.full_url).netloc, "api.heigit.org")
        self.assertEqual(urlsplit(request.full_url).path, "/pelias/v1/search")
        self.assertNotIn(SYNTHETIC_KEY, request.full_url)
        self.assertNotIn("api_key", request.full_url)
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["text"], ["Bedford & N 7th"])
        for key, expected in zip(("min_lon", "min_lat", "max_lon", "max_lat"), BBOX):
            self.assertEqual(float(query["boundary.rect." + key][0]), expected)
        self.assertEqual(query["size"], ["6"])

    def test_search_filters_bad_points_deduplicates_and_caps_six(self):
        bad = [None, {}, point(lon=0), point(lat=float("nan")), point(lon=True),
               point(name="bad\nlabel"), point(name=SYNTHETIC_KEY), point(name=""),
               {"geometry": {"type": "Polygon", "coordinates": [-73.96, 40.715]}, "properties": {"name": "no"}}]
        good = [point(name="Location " + str(i), lat=40.715 + i * .0001) for i in range(8)]
        service, _ = self.service({"features": bad + [good[0]] + good}, key=True)
        result = service.search({"query": "Locations"})
        self.assertEqual(len(result["results"]), 6)
        self.assertEqual(len({r["id"] for r in result["results"]}), 6)
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))

    def test_all_request_methods_reject_unexpected_birth_fields(self):
        service, transport = self.service()
        for method, payload in ((service.weather, {"duration_minutes": 30}),
                                (service.search, {"query": "Bedford"}),
                                (service.reference, route_request())):
            payload["birth_date"] = "1990-05-15"
            with self.assertRaises(ValueError):
                method(payload)
        self.assertFalse(transport.calls)

    def test_query_duration_and_coordinates_reject_invalid_numeric_values(self):
        service, transport = self.service()
        for query in (None, True, [], "x", " " * 3, "x" * 161, "ok\nno"):
            with self.subTest(query_type=type(query).__name__), self.assertRaises(ValueError):
                service.search({"query": query})
        for duration in (None, True, "30", 0, 241, float("nan"), float("inf"), 10**1000):
            with self.subTest(duration_type=type(duration).__name__), self.assertRaises(ValueError):
                service.weather({"duration_minutes": duration})
        for coords in (None, [0, 0], [-73.96, True], [-73.96, float("nan")], [-73.96, 40.715, 1]):
            request = route_request()
            request["origin"] = coords
            with self.assertRaises(ValueError):
                service.reference(request)
        request = route_request()
        request["time_budget"]["stop_minutes"] = 5
        with self.assertRaises(ValueError):
            service.reference(request)
        self.assertFalse(transport.calls)

    def test_provider_errors_are_sanitized_and_not_cached(self):
        for status, reason in ((401, "auth_failed"), (403, "auth_failed"), (429, "quota"),
                               (406, "provider_error"), (503, "provider_error"), (302, "redirect_blocked")):
            for method, payload in (("search", {"query": "Bedford"}), ("reference", route_request())):
                with self.subTest(status=status, method=method):
                    error = HTTPError("https://example.invalid/?api_key=" + SYNTHETIC_KEY,
                                      status, SYNTHETIC_KEY, {}, io.BytesIO(b"raw upstream error body"))
                    service, transport = self.service(key=True, opener=FakeOpener(error=error))
                    result = getattr(service, method)(payload)
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(result["provider_http_status"], status)
                    self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
                    self.assertNotIn("example.invalid", json.dumps(result))
                    self.assertNotIn("raw upstream error body", json.dumps(result))
                    self.assertFalse(service._cache)
                    self.assertEqual(len(transport.calls), 1)

    def test_diagnostic_http_status_only_exposes_valid_integer_codes(self):
        for status in (None, True, False, "406", 406.0, float("nan"), float("inf"), 99, 600, 10**1000):
            with self.subTest(status_type=type(status).__name__, status=repr(status)):
                failure = _ProviderFailure("provider_error", http_status=status)
                service, _ = self.service(opener=FakeOpener(error=failure))
                result = service.weather({"duration_minutes": 30})
                self.assertEqual(result["reason"], "provider_error")
                self.assertNotIn("provider_http_status", result)
                json.dumps(result, allow_nan=False)

    def test_non_success_response_exposes_status_without_reading_raw_body(self):
        response = FakeResponse(b"raw upstream error body " + SYNTHETIC_KEY.encode(),
                                "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson")
        response.status = 503

        def open_response(request, *, timeout):
            self.assertEqual(request.full_url, response.geturl())
            return response

        service, _ = self.service(key=True, opener=open_response)
        result = service.reference(route_request())
        self.assertEqual(result["reason"], "provider_error")
        self.assertEqual(result["provider_http_status"], 503)
        self.assertNotIn("raw upstream error body", json.dumps(result))
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        self.assertFalse(service._cache)

    def test_timeouts_and_connection_failures_return_without_exception_text(self):
        for error, reason in ((TimeoutError(SYNTHETIC_KEY), "timeout"),
                              (URLError(TimeoutError(SYNTHETIC_KEY)), "timeout"),
                              (URLError(SYNTHETIC_KEY), "network"),
                              (ConnectionResetError(SYNTHETIC_KEY), "network")):
            with self.subTest(error_type=type(error).__name__):
                service, _ = self.service(opener=FakeOpener(error=error))
                result = service.weather({"duration_minutes": 30})
                self.assertEqual(result["reason"], reason)
                self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))

    def test_redirect_handler_never_forwards_authorization(self):
        service, _ = self.service({"features": []}, key=True)
        service._opener = FakeOpener({"features": []}, redirected_url="https://other.invalid/")
        self.assertEqual(service.search({"query": "Bedford"})["reason"], "redirect_blocked")
        with self.assertRaises(_ProviderFailure) as caught:
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        self.assertEqual(caught.exception.reason, "redirect_blocked")

    def test_fixed_https_allowlist_rejects_arbitrary_target_before_network(self):
        service, transport = self.service()
        for url in ("http://api.heigit.org/pelias/v1/search", "https://other.invalid/",
                    "https://api.openrouteservice.org/geocode/search",
                    "https://api.openrouteservice.org/v2/directions/foot-walking/geojson",
                    "https://api.heigit.org/geocode/search", "https://api.heigit.org/unknown",
                    "https://api.heigit.org:444/pelias/v1/search",
                    "https://api.open-meteo.com:444/v1/forecast",
                    "https://api.open-meteo.com/v1/forecast#fragment"):
            with self.assertRaises(_ProviderFailure):
                service._request(url, credential=SYNTHETIC_KEY)
        self.assertFalse(transport.calls)

    def test_response_size_json_and_schema_are_bounded(self):
        for data in (b"x" * (MAX_RESPONSE_BYTES + 1), b"not json", [], None,
                     {"features": "bad"}, {"error": {"message": SYNTHETIC_KEY}}):
            with self.subTest(data_type=type(data).__name__):
                service, _ = self.service(data, key=True)
                result = service.search({"query": "Bedford"})
                self.assertEqual(result["reason"], "bad_response")
                self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
                self.assertFalse(service._cache)

    def test_weather_uses_rounded_study_location_no_key_and_explicit_units(self):
        service, transport = self.service(forecast())
        result = service.weather({"duration_minutes": 90})
        self.assertEqual(result["status"], "ok")
        request, _ = transport.calls[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["latitude"], ["40.72"])
        self.assertEqual(query["longitude"], ["-73.96"])
        self.assertEqual(query["timeformat"], ["unixtime"])
        self.assertEqual(query["timezone"], ["UTC"])
        self.assertEqual(query["wind_speed_unit"], ["kmh"])
        self.assertEqual(query["past_days"], ["1"])
        self.assertEqual(query["forecast_days"], ["7"])
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(result["departure_at"], "2026-10-13T10:45:00Z")
        self.assertEqual(result["window_end"], "2026-10-13T12:15:00Z")
        self.assertFalse(result["automatically_changes_weights"])
        self.assertFalse(result["automatically_changes_time_estimate"])

    def test_weather_preceding_hour_rain_buckets_overlap_trip_not_past_hour(self):
        service, _ = self.service(forecast())
        result = service.weather({"duration_minutes": 90})
        self.assertEqual(result["summary"], {"temperature_c": 20, "apparent_temperature_c": 19,
                         "precipitation_probability_max": 70, "precipitation_mm_total": 6,
                         "wind_speed_kmh_max": 13})
        self.assertEqual(result["temperature_sample_at"], "2026-10-13T10:00:00Z")
        self.assertEqual(result["precipitation_bucket_start"], "2026-10-13T10:00:00Z")
        self.assertEqual(result["precipitation_bucket_end"], "2026-10-13T13:00:00Z")
        self.assertEqual(len(result["hours"]), 4)
        self.assertFalse(result["hours"][0]["precipitation_bucket_overlaps_trip"])
        self.assertTrue(all(row["precipitation_bucket_overlaps_trip"] for row in result["hours"][1:]))
        self.assertIn("not_exact_trip", result["precipitation_basis"])

    def test_weather_hour_boundary_does_not_add_adjacent_nonoverlapping_buckets(self):
        start = NOW.replace(minute=0)
        service, _ = self.service(forecast(start), now=lambda: start)
        result = service.weather({"duration_minutes": 60})
        self.assertEqual(result["summary"]["precipitation_mm_total"], 1)
        self.assertEqual(result["summary"]["precipitation_probability_max"], 10)
        self.assertEqual(result["precipitation_bucket_end"], "2026-10-13T11:00:00Z")

    def test_missing_or_invalid_weather_metric_is_null_not_zero_or_partial_sum(self):
        data = forecast()
        data["hourly"]["precipitation"][3] = None
        data["hourly"]["wind_speed_10m"][2] = float("nan")
        del data["hourly"]["apparent_temperature"]
        service, _ = self.service(data)
        result = service.weather({"duration_minutes": 90})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["partial"])
        self.assertIsNone(result["summary"]["precipitation_mm_total"])
        self.assertIsNone(result["summary"]["wind_speed_kmh_max"])
        self.assertIsNone(result["summary"]["apparent_temperature_c"])
        self.assertEqual(result["summary"]["precipitation_probability_max"], 70)
        self.assertEqual(result["summary"]["temperature_c"], 20)
        json.dumps(result, allow_nan=False)

    def test_missing_weather_unit_hides_metric_and_wrong_unit_rejects_response(self):
        data = forecast()
        del data["hourly_units"]["temperature_2m"]
        service, _ = self.service(data)
        self.assertIsNone(service.weather({"duration_minutes": 30})["summary"]["temperature_c"])
        for field, unit in (("time", "iso8601"), ("temperature_2m", "°F"), ("precipitation", "inch"),
                            ("wind_speed_10m", "m/s")):
            data = forecast()
            data["hourly_units"][field] = unit
            service, _ = self.service(data)
            self.assertEqual(service.weather({"duration_minutes": 30})["reason"], "bad_response")

    def test_stale_or_incomplete_forecast_window_is_not_substituted(self):
        for data in (forecast(NOW - timedelta(days=1)), forecast(NOW + timedelta(days=1))):
            service, _ = self.service(data)
            self.assertEqual(service.weather({"duration_minutes": 30})["reason"], "forecast_unavailable")
            self.assertFalse(service._cache)
        data = forecast()
        data["hourly"]["time"][3] += 1
        service, _ = self.service(data)
        self.assertEqual(service.weather({"duration_minutes": 90})["reason"], "bad_response")

    def test_weather_cache_recomputes_departure_window_and_expires(self):
        current = [NOW]
        service, transport = self.service(forecast(), now=lambda: current[0])
        first = service.weather({"duration_minutes": 15})
        current[0] += timedelta(minutes=5)
        second = service.weather({"duration_minutes": 90})
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(first["fetched_at"], second["fetched_at"])
        self.assertNotEqual(first["departure_at"], second["departure_at"])
        self.assertNotEqual(first["window_end"], second["window_end"])
        stamp, data, fetched = service._cache[("weather",)]
        service._cache[("weather",)] = (stamp - 601, data, fetched)
        third = service.weather({"duration_minutes": 30})
        self.assertEqual(len(transport.calls), 2)
        self.assertNotEqual(first["fetched_at"], third["fetched_at"])

    def test_midnight_four_hour_forecast_window_is_covered(self):
        departure = NOW.replace(hour=23, minute=59)
        service, _ = self.service(forecast(departure), now=lambda: departure)
        result = service.weather({"duration_minutes": 240})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["window_end"], "2026-10-14T03:59:00Z")
        self.assertEqual(result["precipitation_bucket_end"], "2026-10-14T04:00:00Z")

    def test_reference_preserves_provider_eta_separate_from_local_speed(self):
        service, transport = self.service(route_response(), key=True)
        request = route_request()
        result = service.reference(request)
        self.assertEqual(result["walking_minutes"], 15)
        self.assertEqual(result["total_minutes"], 18)
        self.assertEqual(result["distance_m"], 1200)
        self.assertFalse(result["same_route_as_recommendation"])
        self.assertFalse(result["user_walking_speed_applied"])
        self.assertTrue(result["within_time_budget"])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        outbound, timeout = transport.calls[0]
        self.assertEqual(outbound.get_method(), "POST")
        self.assertEqual(outbound.full_url, "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson")
        body = json.loads(outbound.data)
        self.assertEqual(set(body), {"coordinates", "instructions", "units"})
        self.assertEqual(body["coordinates"], [request["origin"], request["destination"]])
        self.assertEqual(body["units"], "m")
        request["time_budget"]["walking_speed_kmh"] = 5.5
        request["time_budget"]["max_minutes"] = 17.9
        updated = service.reference(request)
        self.assertEqual(updated["walking_minutes"], 15)
        self.assertFalse(updated["within_time_budget"])
        self.assertEqual(len(transport.calls), 1)

    def test_reference_negotiates_geojson_on_current_endpoint(self):
        class ContentNegotiatingOpener(FakeOpener):
            def open(self, request, *, timeout):
                accepted = {value.strip() for value in request.get_header("Accept", "").split(",")}
                if "application/geo+json" not in accepted:
                    raise HTTPError(request.full_url, 406, "Not Acceptable", {}, None)
                return super().open(request, timeout=timeout)

        transport = ContentNegotiatingOpener(route_response())
        service, _ = self.service(key=True, opener=transport)
        result = service.reference(route_request())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["walking_minutes"], 15)
        self.assertEqual(len(transport.calls), 1)
        request, _ = transport.calls[0]
        self.assertEqual(request.full_url,
                         "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson")
        self.assertEqual(request.get_header("Accept"), "application/geo+json, application/json")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), SYNTHETIC_KEY)

    def test_reference_via_order_and_dwell_buffer_recomputed_from_cached_route(self):
        service, transport = self.service(route_response(), key=True)
        request = route_request()
        request["via"] = [-73.962, 40.713]
        request["time_budget"]["stop_minutes"] = 8
        result = service.reference(request)
        self.assertEqual(json.loads(transport.calls[0][0].data)["coordinates"],
                         [request["origin"], request["via"], request["destination"]])
        self.assertEqual(result["total_minutes"], 26)
        request["time_budget"]["buffer_minutes"] = 5
        self.assertEqual(service.reference(request)["total_minutes"], 28)
        self.assertEqual(len(transport.calls), 1)

    def test_reference_rejects_invalid_geometry_and_duration_without_raw_body(self):
        cases = []
        for value in (-1, True, None, float("nan"), 10**1000):
            data = route_response()
            data["features"][0]["properties"]["summary"]["duration"] = value
            cases.append(data)
        for geometry in ({"type": "Point", "coordinates": [0, 0]},
                         {"type": "LineString", "coordinates": [[-73.96, 40.71], [0, float("inf")]]},
                         {"type": "LineString", "coordinates": [[-73.96, 40.71]]}):
            data = route_response()
            data["features"][0]["geometry"] = geometry
            cases.append(data)
        for data in cases:
            service, _ = self.service(data, key=True)
            result = service.reference(route_request())
            self.assertEqual(result["reason"], "bad_response")
            self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
            self.assertFalse(service._cache)

    def test_cache_is_bounded_copied_and_reset_when_session_changes(self):
        service, transport = self.service({"features": [point()]}, key=True)
        result = service.search({"query": "Location zero"})
        result["results"][0]["name"] = "mutation"
        self.assertNotEqual(service.search({"query": "Location zero"})["results"][0]["name"], "mutation")
        self.assertEqual(len(transport.calls), 1)
        for i in range(MAX_CACHE_ENTRIES + 5):
            service.search({"query": "Location " + str(i)})
        self.assertEqual(len(service._cache), MAX_CACHE_ENTRIES)
        service.set_ors_key({"ors_api_key": ""})
        self.assertFalse(service._cache)
        self.assertEqual(service.config()["ors"]["key_source"], "none")

    def test_busy_limiter_fails_without_starting_another_network_request(self):
        service, transport = self.service(forecast())
        service._slots.acquire()
        service._slots.acquire()
        result = service.weather({"duration_minutes": 30})
        self.assertEqual(result["reason"], "busy")
        self.assertFalse(transport.calls)
        service._slots.release()
        service._slots.release()


if __name__ == "__main__":
    unittest.main()
