"""Shared validated input context for independent saju and astrology analyses.

The returned context contains private date/time objects for in-process use only.
It must not be serialized into an HTTP response, route request, or export. No
input is stored, logged, geocoded, or sent to an external service here. Missing
birth coordinates remain missing; New York is only the analysis-day timezone.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
import math
from zoneinfo import ZoneInfo


TARGET_TIMEZONE = "America/New_York"
ALLOWED_FIELDS = frozenset({
    "birth_date", "birth_time", "birth_timezone", "birth_fold", "target_date",
    "target_timezone", "birth_latitude", "birth_longitude", "house_system",
})


class AnalysisInputError(ValueError):
    """Input-free Korean error safe for the local API's validation response."""


def _coordinates(payload):
    has_latitude = "birth_latitude" in payload
    has_longitude = "birth_longitude" in payload
    if has_latitude != has_longitude:
        raise AnalysisInputError("출생지 위도와 경도를 함께 입력하거나 둘 다 비워 주세요.")
    if not has_latitude:
        return None, None
    values = []
    for field, limit, label in (("birth_latitude", 90, "위도"),
                                ("birth_longitude", 180, "경도")):
        value = payload[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not -limit <= value <= limit or not math.isfinite(value)):
            raise AnalysisInputError(f"출생지 {label}는 -{limit}부터 {limit} 사이의 유한한 숫자로 입력해 주세요.")
        values.append(float(value))
    return tuple(values)


def prepare_input(payload):
    """Validate once and resolve civil times into a shared internal context.

    Known birth times become one real aware datetime. Missing times retain all
    hourly samples and the last valid second of the civil day (including both
    DST folds), clipped to the present for a birth date still in progress.
    Each analysis decides which facts remain stable across those samples.
    """
    if not isinstance(payload, dict):
        raise AnalysisInputError("출생 정보를 JSON 객체로 입력해 주세요.")
    if set(payload) - ALLOWED_FIELDS:
        raise AnalysisInputError("지원하지 않는 출생 정보 항목이 있어요.")
    if not {"birth_date", "birth_timezone", "target_date"} <= set(payload):
        raise AnalysisInputError("생년월일, 출생지 시간대와 분석 날짜를 입력해 주세요.")
    if payload.get("target_timezone", TARGET_TIMEZONE) != TARGET_TIMEZONE:
        raise AnalysisInputError("이 지도는 뉴욕 시간대를 기준으로 분석해요.")
    fold = payload.get("birth_fold")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise AnalysisInputError("중복 출생 시간은 첫 번째 또는 두 번째를 선택해 주세요.")
    house_system = payload.get("house_system", "P")
    if house_system not in ("P", "W"):
        raise AnalysisInputError("하우스 방식은 P 또는 W를 선택해 주세요.")
    latitude, longitude = _coordinates(payload)

    # Reuse the existing civil-time boundary handling, never its blended
    # build_profile. Lazy import permits astro_profile to delegate to this
    # module while those calendar helpers remain in their original location.
    from .astro_profile import (
        AstroProfileError, _date_value, _known_local, _timezone_value,
        _whole_day_instants,
    )

    now = datetime.now(timezone.utc)
    try:
        zone = _timezone_value(payload["birth_timezone"])
        day = _date_value(payload["birth_date"], birth=True, now=now)
        target_day = _date_value(payload["target_date"], birth=False, now=now)
        clock_text = payload.get("birth_time")
        if clock_text is None:
            local = None
            samples, hours, considered, clipped = _whole_day_instants(day, zone, now)
            window = {
                "time_known": False, "sample_count": len(samples),
                "civil_day_hours": hours, "birth_window_hours": considered,
                "time_window_clipped_to_now": clipped,
                "sampling": "hourly_and_last_valid_second",
                "note": ("출생일이 오늘이므로 현지 날짜 시작부터 현재 순간까지만 비교했어요."
                         if clipped else "출생지의 현지 날짜 전체를 비교했어요."),
            }
        else:
            local = _known_local(day, clock_text, zone, fold)
            if local.astimezone(timezone.utc) > now:
                raise AnalysisInputError("출생 시간은 현재보다 이전이어야 해요.")
            samples = [local]
            window = {
                "time_known": True, "sample_count": 1, "civil_day_hours": None,
                "birth_window_hours": 0.0, "time_window_clipped_to_now": False,
                "sampling": "known_instant", "note": "입력한 출생 시각 한 순간을 계산했어요.",
            }
    except AstroProfileError as exc:
        raise AnalysisInputError(str(exc)) from None

    target_local = datetime.combine(target_day, time(12), ZoneInfo(TARGET_TIMEZONE))
    return {
        "birth_date": day, "birth_timezone": zone, "birth_local": local,
        "birth_samples": samples, "birth_window": window,
        "target_local": target_local, "latitude": latitude,
        "longitude": longitude, "house_system": house_system,
    }
