"""Small, optional server-side adapters for ORS and Open-Meteo.

No API keys, raw provider bodies, birth information or request URLs are logged,
persisted or returned. External durations never replace our own route estimate.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .travel_time import parse_time_budget


ORS_BASE = "https://api.heigit.org/openrouteservice"
ORS_SEARCH_URL = "https://api.heigit.org/pelias/v1/search"
ORS_REFERENCE_URL = ORS_BASE + "/v2/directions/foot-walking/geojson"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 64
WEATHER_TTL_SECONDS = 600
ORS_TTL_SECONDS = 300
ENV_KEYS = ("LUCKYPATH_ORS_API_KEY", "ORS_API_KEY", "OPENROUTESERVICE_API_KEY")
WEATHER_FIELDS = ("temperature_2m", "apparent_temperature", "precipitation_probability",
                  "precipitation", "wind_speed_10m")
WEATHER_UNITS = {"time": "unixtime", "temperature_2m": "°C", "apparent_temperature": "°C",
                 "precipitation_probability": "%", "precipitation": "mm", "wind_speed_10m": "km/h"}
ORS_ATTRIBUTION = [{"name": "openrouteservice / HeiGIT", "url": "https://openrouteservice.org/"},
                   {"name": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"}]
WEATHER_ATTRIBUTION = [{"name": "Weather data by Open-Meteo", "url": "https://open-meteo.com/"},
                       {"name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}]
MESSAGES = {
    "missing_key": "openrouteservice API 키를 연결하면 주소 검색과 외부 보행 경로 비교를 사용할 수 있어요.",
    "auth_failed": "openrouteservice 키 인증에 실패했어요. 키와 계정의 사용 권한을 확인해 주세요.",
    "quota": "외부 서비스의 요청 한도에 도달했어요. 잠시 후 다시 시도해 주세요.",
    "timeout": "외부 서비스 응답이 늦어 조회를 중단했어요. 로컬 경로는 계속 사용할 수 있어요.",
    "network": "외부 서비스에 연결하지 못했어요. 로컬 경로는 계속 사용할 수 있어요.",
    "provider_error": "외부 서비스가 요청을 처리하지 못했어요. 로컬 경로는 계속 사용할 수 있어요.",
    "bad_response": "외부 서비스의 응답을 확인할 수 없어 이번 결과를 표시하지 않았어요.",
    "forecast_unavailable": "현재 이동 시간대를 포함하는 예보를 받지 못했어요.",
    "redirect_blocked": "외부 서비스의 예상하지 못한 연결 변경을 차단했어요.",
    "busy": "외부 정보를 조회하고 있어요. 잠시 후 다시 시도해 주세요.",
}


class _ProviderFailure(Exception):
    def __init__(self, reason, http_status=None):
        self.reason = reason
        self.http_status = (http_status if type(http_status) is int and 100 <= http_status <= 599 else None)
        super().__init__(reason)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward Authorization, even to another path on the same host.
        raise _ProviderFailure("redirect_blocked")


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and low <= result <= high else None


def _object(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError("요청 항목을 확인해 주세요.")


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(value):
    if not isinstance(value, str):
        raise ValueError("ORS API 키는 문자열이어야 해요.")
    value = value.strip()
    if len(value) > 512 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("ORS API 키 형식을 확인해 주세요.")
    return value


class ExternalServices:
    """The injectable opener supports .open(Request, timeout=...) or a callable.

    A maximum of two simultaneous provider requests is allowed; successful
    responses live only in a bounded memory cache. Failed calls are not retried.
    """
    def __init__(self, bbox, *, opener=None, now=None):
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(_number(n, -180 if i % 2 == 0 else -90,
                               180 if i % 2 == 0 else 90) is None for i, n in enumerate(bbox))
                or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ValueError("A valid study bounding box is required")
        self.bbox = [float(n) for n in bbox]
        self._opener = opener or build_opener(_NoRedirect())
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(2)
        self._session_key = None
        self._environment_key = None
        for name in ENV_KEYS:
            value = os.environ.get(name)
            if value:
                try:
                    self._environment_key = _key(value) or None
                except ValueError:
                    continue
                if self._environment_key:
                    break
        self._key_generation = 0
        self._cache = OrderedDict()

    def _credential(self):
        with self._lock:
            if self._session_key:
                return self._session_key, "session", self._key_generation
            if self._environment_key:
                return self._environment_key, "environment", self._key_generation
            return None, "none", self._key_generation

    def config(self):
        credential, source, _ = self._credential()
        return {"ors": {"provider": "openrouteservice", "configured": bool(credential),
                        "key_source": source, "key_validated": False,
                        "key_validation": "only_when_provider_is_called",
                        "session_key_persisted": False,
                        "blank_session_key_uses_environment": True,
                        "signup_url": "https://account.heigit.org/",
                        "search_bbox": list(self.bbox), "reference_profile": "foot-walking"},
                "weather": {"provider": "open-meteo", "enabled": True,
                            "api_key_required": False, "departure": "server_current_time",
                            "location": self._weather_location(), "location_precision_degrees": 0.01,
                            "cache_seconds": WEATHER_TTL_SECONDS,
                            "commercial_use_requires_plan_review": True,
                            "automatically_changes_weights": False,
                            "automatically_changes_time_estimate": False}}

    def set_ors_key(self, payload):
        _object(payload, {"ors_api_key"}, {"ors_api_key"})
        value = _key(payload["ors_api_key"])
        with self._lock:
            self._session_key = value or None
            self._key_generation += 1
            self._cache = OrderedDict((k, v) for k, v in self._cache.items() if k[0] == "weather")
        return self.config()

    def _weather_location(self):
        return [round((self.bbox[0] + self.bbox[2]) / 2, 2),
                round((self.bbox[1] + self.bbox[3]) / 2, 2)]

    def _coordinate(self, value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("좌표는 [경도, 위도]로 입력해 주세요.")
        lon, lat = _number(value[0], -180, 180), _number(value[1], -90, 90)
        if (lon is None or lat is None or not self.bbox[0] <= lon <= self.bbox[2]
                or not self.bbox[1] <= lat <= self.bbox[3]):
            raise ValueError("현재 지원 지역 안의 좌표를 선택해 주세요.")
        return [lon, lat]

    @staticmethod
    def _unavailable(provider, reason, http_status=None):
        result = {"status": "unavailable", "provider": provider, "reason": reason,
                "message": MESSAGES[reason],
                "attribution": deepcopy(WEATHER_ATTRIBUTION if provider == "open-meteo" else ORS_ATTRIBUTION)}
        if type(http_status) is int and 100 <= http_status <= 599:
            result["provider_http_status"] = http_status
        return result

    def _request(self, url, *, body=None, credential=None):
        parts = urlsplit(url)
        allowed = ((parts.netloc == "api.open-meteo.com" and parts.path == "/v1/forecast")
                   or (parts.netloc == "api.heigit.org"
                       and parts.path in {"/pelias/v1/search", "/openrouteservice/v2/directions/foot-walking/geojson"}))
        if parts.scheme != "https" or not allowed or parts.fragment or parts.username:
            raise _ProviderFailure("network")
        headers = {"Accept": "application/json", "User-Agent": "LuckyPath-local/0.3"}
        if url == ORS_REFERENCE_URL:
            # The GeoJSON handler does not accept application/json alone.
            headers["Accept"] = "application/geo+json, application/json"
        if credential:
            headers["Authorization"] = credential
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        if not self._slots.acquire(timeout=0.1):
            raise _ProviderFailure("busy")
        try:
            opening = getattr(self._opener, "open", self._opener)
            with opening(request, timeout=TIMEOUT_SECONDS) as response:
                if response.geturl() != url:
                    raise _ProviderFailure("redirect_blocked")
                status = getattr(response, "status", 200)
                if status != 200:
                    raise _ProviderFailure(self._http_reason(status), status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _ProviderFailure("bad_response")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise _ProviderFailure("bad_response")
            return result
        except HTTPError as exc:
            reason = self._http_reason(exc.code)
            exc.close()
            raise _ProviderFailure(reason, exc.code) from None
        except (TimeoutError, socket.timeout):
            raise _ProviderFailure("timeout") from None
        except URLError as exc:
            reason = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network"
            raise _ProviderFailure(reason) from None
        except OSError:
            raise _ProviderFailure("network") from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _ProviderFailure("bad_response") from None
        finally:
            self._slots.release()

    @staticmethod
    def _http_reason(status):
        if status in (401, 403):
            return "auth_failed"
        if status == 429:
            return "quota"
        if 300 <= status < 400:
            return "redirect_blocked"
        return "provider_error"

    def _cached(self, cache_key, ttl, fetch):
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry and 0 <= time.monotonic() - entry[0] < ttl:
                self._cache.move_to_end(cache_key)
                return deepcopy(entry[1]), entry[2]
            self._cache.pop(cache_key, None)
        result = fetch()
        fetched_at = self._now()
        with self._lock:
            self._cache[cache_key] = (time.monotonic(), deepcopy(result), fetched_at)
            while len(self._cache) > MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return result, fetched_at

    def _evict(self, cache_key):
        with self._lock:
            self._cache.pop(cache_key, None)

    def search(self, payload):
        _object(payload, {"query"}, {"query"})
        query = payload["query"]
        if (not isinstance(query, str) or not 2 <= len(query.strip()) <= 160
                or any(ord(char) < 32 or ord(char) == 127 for char in query)):
            raise ValueError("검색어는 2~160자로 입력해 주세요.")
        query = query.strip()
        credential, _, generation = self._credential()
        if not credential:
            return dict(self._unavailable("openrouteservice", "missing_key"), results=[])
        params = {"text": query, "size": 6,
                  "boundary.rect.min_lon": self.bbox[0], "boundary.rect.min_lat": self.bbox[1],
                  "boundary.rect.max_lon": self.bbox[2], "boundary.rect.max_lat": self.bbox[3]}
        url = ORS_SEARCH_URL + "?" + urlencode(params)
        try:
            data, fetched = self._cached(("search", generation, query.casefold()), ORS_TTL_SECONDS,
                                        lambda: self._request(url, credential=credential))
            if not isinstance(data.get("features"), list):
                raise _ProviderFailure("bad_response")
            results, seen = [], set()
            for feature in data["features"][:100]:
                if not isinstance(feature, dict):
                    continue
                geometry, properties = feature.get("geometry"), feature.get("properties")
                if not isinstance(geometry, dict) or geometry.get("type") != "Point" or not isinstance(properties, dict):
                    continue
                try:
                    point = self._coordinate(geometry.get("coordinates"))
                except ValueError:
                    continue
                name = properties.get("label") or properties.get("name")
                if (not isinstance(name, str) or not name.strip() or len(name) > 500 or credential in name
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    continue
                name = name.strip()[:240]
                identity = (name, *point)
                if identity in seen:
                    continue
                seen.add(identity)
                results.append({"id": "ors:" + hashlib.sha256(repr(identity).encode()).hexdigest()[:16],
                                "name": name, "lonlat": point})
                if len(results) == 6:
                    break
            return {"status": "ok", "provider": "openrouteservice", "results": results,
                    "fetched_at": _iso(fetched), "attribution": deepcopy(ORS_ATTRIBUTION)}
        except _ProviderFailure as exc:
            self._evict(("search", generation, query.casefold()))
            return dict(self._unavailable("openrouteservice", exc.reason, exc.http_status), results=[])

    def weather(self, payload):
        _object(payload, {"duration_minutes"}, {"duration_minutes"})
        duration = _number(payload["duration_minutes"], 1, 240)
        if duration is None:
            raise ValueError("날씨 조회 시간은 1~240분으로 입력해 주세요.")
        departure = self._now().astimezone(timezone.utc)
        window_end = departure + timedelta(minutes=duration)
        lon, lat = self._weather_location()
        params = {"latitude": lat, "longitude": lon, "hourly": ",".join(WEATHER_FIELDS),
                  "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
                  "timezone": "UTC", "timeformat": "unixtime", "past_days": 1, "forecast_days": 7}
        try:
            data, fetched = self._cached(("weather",), WEATHER_TTL_SECONDS,
                                        lambda: self._request(WEATHER_URL + "?" + urlencode(params)))
            return self._weather_result(data, departure, window_end, fetched)
        except _ProviderFailure as exc:
            self._evict(("weather",))
            return self._unavailable("open-meteo", exc.reason, exc.http_status)

    def _weather_result(self, data, departure, window_end, fetched):
        hourly, units = data.get("hourly"), data.get("hourly_units")
        if (not isinstance(hourly, dict) or not isinstance(units, dict)
                or units.get("time") != "unixtime"
                or any(field in units and units[field] != unit for field, unit in WEATHER_UNITS.items())
                or data.get("utc_offset_seconds") != 0):
            raise _ProviderFailure("bad_response")
        times = hourly.get("time")
        if (not isinstance(times, list) or not 2 <= len(times) <= 500
                or any(_number(t, 0, 10**11) is None or t % 3600 != 0 for t in times)
                or times != sorted(set(times))):
            raise _ProviderFailure("bad_response")
        start_s, end_s = departure.timestamp(), window_end.timestamp()
        first, last = math.floor(start_s / 3600) * 3600, math.ceil(end_s / 3600) * 3600
        expected = list(range(first, last + 1, 3600))
        indices = {stamp: i for i, stamp in enumerate(times)}
        if any(stamp not in indices for stamp in expected):
            raise _ProviderFailure("forecast_unavailable")
        ranges = {"temperature_2m": (-100, 70), "apparent_temperature": (-150, 100),
                  "precipitation_probability": (0, 100), "precipitation": (0, 1000),
                  "wind_speed_10m": (0, 500)}
        def metric(field, stamp):
            values = hourly.get(field)
            if (units.get(field) != WEATHER_UNITS[field]
                    or not isinstance(values, list) or len(values) != len(times)):
                return None
            return _number(values[indices[stamp]], *ranges[field])
        precip_stamps = [stamp for stamp in expected if stamp > start_s and stamp - 3600 < end_s]
        def aggregate(field, stamps, operation):
            values = [metric(field, stamp) for stamp in stamps]
            return operation(values) if values and all(value is not None for value in values) else None
        summary = {"temperature_c": metric("temperature_2m", first),
                   "apparent_temperature_c": metric("apparent_temperature", first),
                   "precipitation_probability_max": aggregate("precipitation_probability", precip_stamps, max),
                   "precipitation_mm_total": aggregate("precipitation", precip_stamps, math.fsum),
                   "wind_speed_kmh_max": aggregate("wind_speed_10m", expected, max)}
        messages = ["지금 출발하는 이동의 시간별 예보예요. 실제 거리의 관측값과 다를 수 있어요.",
                    "강수량·확률은 이동 시간과 겹치는 한 시간 단위 예보예요. 강수량 합계는 정확한 이동 시간 동안의 강수량이 아니에요."]
        if any(value is None for value in summary.values()):
            messages.append("일부 예보가 누락되어 해당 항목을 표시하지 않았어요.")
        if summary["precipitation_probability_max"] is not None and summary["precipitation_probability_max"] >= 50:
            messages.append("겹치는 시간대 중 강수 확률이 50% 이상인 구간이 있어요.")
        return {"status": "ok", "provider": "open-meteo", "departure_at": _iso(departure),
                "window_end": _iso(window_end), "fetched_at": _iso(fetched),
                "location": self._weather_location(), "summary": summary,
                "temperature_sample_at": _iso(datetime.fromtimestamp(first, timezone.utc)),
                "precipitation_basis": "overlapping_preceding_hour_buckets_not_exact_trip_total",
                "precipitation_bucket_start": _iso(datetime.fromtimestamp(precip_stamps[0] - 3600, timezone.utc)),
                "precipitation_bucket_end": _iso(datetime.fromtimestamp(precip_stamps[-1], timezone.utc)),
                "hours": [{"time": _iso(datetime.fromtimestamp(stamp, timezone.utc)),
                           **{field: metric(field, stamp) for field in WEATHER_FIELDS},
                           "precipitation_bucket_overlaps_trip": stamp in precip_stamps} for stamp in expected],
                "messages": messages, "partial": any(value is None for value in summary.values()),
                "automatically_changes_weights": False, "automatically_changes_time_estimate": False,
                "attribution": deepcopy(WEATHER_ATTRIBUTION)}

    def reference(self, payload):
        _object(payload, {"origin", "destination", "via", "time_budget"},
                {"origin", "destination", "time_budget"})
        origin, destination = self._coordinate(payload["origin"]), self._coordinate(payload["destination"])
        via = self._coordinate(payload["via"]) if "via" in payload else None
        budget = parse_time_budget(payload["time_budget"], has_via=via is not None)
        coordinates = [origin] + ([via] if via is not None else []) + [destination]
        credential, _, generation = self._credential()
        if not credential:
            return self._unavailable("openrouteservice", "missing_key")
        body = {"coordinates": coordinates, "instructions": False, "units": "m"}
        try:
            data, fetched = self._cached(("reference", generation, json.dumps(coordinates)), ORS_TTL_SECONDS,
                                        lambda: self._request(ORS_REFERENCE_URL,
                                                              body=body, credential=credential))
            features = data.get("features")
            if not isinstance(features, list) or not features or not isinstance(features[0], dict):
                raise _ProviderFailure("bad_response")
            feature = features[0]
            properties, geometry = feature.get("properties"), feature.get("geometry")
            summary = properties.get("summary") if isinstance(properties, dict) else None
            if not isinstance(summary, dict) or not isinstance(geometry, dict) or geometry.get("type") != "LineString":
                raise _ProviderFailure("bad_response")
            distance, seconds = _number(summary.get("distance"), 0, 500000), _number(summary.get("duration"), 0, 259200)
            points = geometry.get("coordinates")
            if (distance is None or seconds is None or (distance > 0 and seconds == 0)
                    or not isinstance(points, list) or not 2 <= len(points) <= 20000
                    or any(not isinstance(point, list) or len(point) != 2
                           or _number(point[0], -180, 180) is None or _number(point[1], -90, 90) is None for point in points)):
                raise _ProviderFailure("bad_response")
            walking = seconds / 60
            total = math.fsum((walking, budget["buffer_minutes"], budget["stop_minutes"]))
            return {"status": "ok", "provider": "openrouteservice", "profile": "foot-walking",
                    "distance_m": distance, "walking_minutes": walking, "total_minutes": total,
                    "buffer_minutes": budget["buffer_minutes"], "stop_minutes": budget["stop_minutes"],
                    "within_time_budget": total <= budget["max_minutes"],
                    "user_walking_speed_applied": False, "same_route_as_recommendation": False,
                    "arrival_guaranteed": False, "fetched_at": _iso(fetched),
                    "provider_route_feature": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": points},
                                               "properties": {"provider": "openrouteservice", "distance_m": distance,
                                                              "walking_minutes": walking}},
                    "message": "외부 기준 경로의 예상 시간이에요. 추천 경로의 시간과 별도로 비교해 주세요.",
                    "attribution": deepcopy(ORS_ATTRIBUTION)}
        except _ProviderFailure as exc:
            self._evict(("reference", generation, json.dumps(coordinates)))
            return self._unavailable("openrouteservice", exc.reason, exc.http_status)
