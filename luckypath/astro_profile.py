"""Calendar/astronomy inputs for an explicitly symbolic walking profile.

Year/month pillars use lunar-python's exact solar terms in fixed UTC+08:00.
Day/hour pillars use the birthplace's civil wall clock with sect=2. This is a
deliberate mixed calendar convention, not true solar time or a yongshin reading.
The separate astro_policy module owns every interpretation and mixing weight.
No birth input is stored here or included verbatim in an error message.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from importlib.metadata import version
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import astronomy
from lunar_python import Solar
from lunar_python.util import LunarUtil


PROFILE_VERSION = "saju-zodiac-symbolic-v1"
TARGET_TIMEZONE = "America/New_York"
_UTC8 = timezone(timedelta(hours=8))
_UTC = timezone.utc
_PILLAR_KEYS = ("year", "month", "day", "hour")
_BODY_KEYS = ("sun", "moon")
_ELEMENTS = {"木": "wood", "火": "fire", "土": "earth", "金": "metal", "水": "water"}
_SIGNS = (
    ("aries", "양자리", "fire"), ("taurus", "황소자리", "earth"),
    ("gemini", "쌍둥이자리", "air"), ("cancer", "게자리", "water"),
    ("leo", "사자자리", "fire"), ("virgo", "처녀자리", "earth"),
    ("libra", "천칭자리", "air"), ("scorpio", "전갈자리", "water"),
    ("sagittarius", "사수자리", "fire"), ("capricorn", "염소자리", "earth"),
    ("aquarius", "물병자리", "air"), ("pisces", "물고기자리", "water"),
)


class AstroProfileError(ValueError):
    """A concise, input-free Korean explanation suitable for the local API."""


def _date_value(value, *, birth, now):
    label = "생년월일" if birth else "분석 날짜"
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AstroProfileError(f"{label}를 YYYY-MM-DD 형식으로 입력해 주세요.")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise AstroProfileError(f"{label}에 실제로 존재하는 날짜를 입력해 주세요.") from None
    # Match the form's date maximum even when a birthplace is a civil day ahead
    # of New York. The actual instant / local-day start is checked separately.
    upper = now.astimezone(ZoneInfo("Pacific/Kiritimati")).date() if birth else date(2100, 12, 31)
    if not date(1900, 1, 1) <= result <= upper:
        message = ("생년월일은 1900년부터 오늘까지 입력할 수 있어요."
                   if birth else "분석 날짜는 1900년부터 2100년까지 입력할 수 있어요.")
        raise AstroProfileError(message)
    return result


def _timezone_value(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise AstroProfileError("출생지 시간대를 선택해 주세요.")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise AstroProfileError("출생지 시간대를 확인해 주세요.") from None


def _valid_local_candidates(naive, zone):
    """Round-trip both folds; assigning tzinfo alone does not detect DST gaps."""
    by_instant = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(_UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            by_instant.setdefault(utc, aware)
    return [by_instant[key] for key in sorted(by_instant)]


def _known_local(day, clock_text, zone, fold):
    if not isinstance(clock_text, str) or not re.fullmatch(r"\d{2}:\d{2}", clock_text):
        raise AstroProfileError("출생 시간을 HH:MM 형식으로 입력하거나 모름을 선택해 주세요.")
    try:
        clock = time.fromisoformat(clock_text)
    except ValueError:
        raise AstroProfileError("출생 시간의 시와 분을 확인해 주세요.") from None
    naive = datetime.combine(day, clock)
    candidates = _valid_local_candidates(naive, zone)
    if not candidates:
        raise AstroProfileError("서머타임 변경으로 존재하지 않는 출생 시간이에요. 시간을 확인해 주세요.")
    if len(candidates) > 1:
        if fold is None:
            raise AstroProfileError("서머타임 종료로 같은 시간이 두 번 있어요. 첫 번째인지 두 번째인지 선택해 주세요.")
        return naive.replace(tzinfo=zone, fold=fold)
    return candidates[0]


def _day_edge(day, zone, *, last):
    """Find the first/last valid civil second, including midnight offset changes."""
    midnight = datetime.combine(day, time())
    minute_offsets = range(1439, -1, -1) if last else range(1440)
    for minute in minute_offsets:
        offset = minute * 60 + (59 if last else 0)
        candidates = _valid_local_candidates(midnight + timedelta(seconds=offset), zone)
        if not candidates:
            continue
        # Refine to a second when a historical transition occurs between minutes.
        seconds = (range(min(86399, offset + 59), offset - 1, -1)
                   if last else range(max(0, offset - 59), offset + 1))
        for second in seconds:
            precise = _valid_local_candidates(midnight + timedelta(seconds=second), zone)
            if precise:
                return (precise[-1] if last else precise[0]).astimezone(_UTC)
    raise AstroProfileError("선택한 시간대에서 존재하지 않는 출생 날짜예요. 날짜와 시간대를 확인해 주세요.")


def _whole_day_instants(day, zone, now):
    start = _day_edge(day, zone, last=False)
    civil_end = _day_edge(day, zone, last=True)
    if start > now:
        raise AstroProfileError("출생 날짜는 현재보다 이전이어야 해요. 출생지 시간대를 확인해 주세요.")
    end = min(civil_end, now.replace(microsecond=0))
    samples = []
    instant = start
    while instant <= end:
        local = instant.astimezone(zone)
        if local.date() == day:
            samples.append(local)
        instant += timedelta(hours=1)
    if not samples or samples[-1].astimezone(_UTC) != end:
        samples.append(end.astimezone(zone))
    civil_stop = civil_end + timedelta(seconds=1)
    return (samples,
            (civil_stop - start).total_seconds() / 3600,
            (min(civil_stop, now) - start).total_seconds() / 3600,
            now < civil_stop)


def _pillar(text):
    stem, branch = text[0], text[1]
    return {
        "text": text, "stem": stem, "branch": branch,
        "stem_element": _ELEMENTS[LunarUtil.WU_XING_GAN[stem]],
        "branch_element": _ELEMENTS[LunarUtil.WU_XING_ZHI[branch]],
    }


def _sign(index):
    key, label, element = _SIGNS[index]
    return {"sign_index": index, "sign_key": key, "sign": label, "element": element}


def _body(longitude):
    longitude = float(longitude) % 360
    if not math.isfinite(longitude):
        raise AstroProfileError("천체 위치를 계산하지 못했어요. 입력을 다시 확인해 주세요.")
    return {"longitude_deg": longitude, **_sign(int(longitude // 30))}


def _chart_at(local):
    boundary = Solar.fromDate(local.astimezone(_UTC8)).getLunar().getEightChar()
    civil = Solar.fromDate(local).getLunar().getEightChar()
    civil.setSect(2)
    utc = local.astimezone(_UTC)
    instant = astronomy.Time.Make(utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second)
    return {
        "pillars": {
            "year": _pillar(boundary.getYear()), "month": _pillar(boundary.getMonth()),
            "day": _pillar(civil.getDay()), "hour": _pillar(civil.getTime()),
        },
        "sun": _body(astronomy.SunPosition(instant).elon),
        "moon": _body(astronomy.EclipticGeoMoon(instant).lon),
    }


def _unique(values, key):
    found = {}
    for value in values:
        found.setdefault(value[key], value)
    return list(found.values())


def _known_natal(local):
    chart = _chart_at(local)
    chart["time_known"] = True
    chart["alternatives"] = {
        "pillars": {key: [chart["pillars"][key].copy()] for key in _PILLAR_KEYS},
        "sun_signs": [_sign(chart["sun"]["sign_index"])],
        "moon_signs": [_sign(chart["moon"]["sign_index"])],
    }
    return chart, []


def _unknown_natal(day, zone, now):
    instants, hours, considered_hours, clipped = _whole_day_instants(day, zone, now)
    charts = [_chart_at(instant) for instant in instants]
    chart = {"time_known": False, "civil_day_hours": hours, "pillars": {},
             "birth_window_hours": considered_hours,
             "time_window_clipped_to_now": clipped,
             "time_window_note": ("출생일이 오늘이므로 현지 날짜 시작부터 현재 순간까지만 비교했어요."
                                  if clipped else "출생지의 현지 날짜 전체를 비교했어요."),
             "alternatives": {"pillars": {}}, "longitude_ranges_deg": {}}
    omissions = [{"field": "natal.hour", "reason": "출생 시간을 몰라 시주는 제외했어요."}]
    labels = {"year": "연주", "month": "월주", "day": "일주"}
    for key in _PILLAR_KEYS:
        if key == "hour":
            chart["pillars"][key] = None
            chart["alternatives"]["pillars"][key] = []
            continue
        options = _unique([item["pillars"][key] for item in charts], "text")
        chart["alternatives"]["pillars"][key] = options
        chart["pillars"][key] = options[0].copy() if len(options) == 1 else None
        if len(options) > 1:
            omissions.append({"field": f"natal.{key}",
                              "reason": f"출생일 안에서 {labels[key]}가 바뀌어 해당 요소는 제외했어요."})
    for key, label in (("sun", "태양"), ("moon", "달")):
        values = [item[key] for item in charts]
        options = _unique([_sign(item["sign_index"]) for item in values], "sign_index")
        chart["alternatives"][f"{key}_signs"] = options
        chart[key] = {"longitude_deg": None, **options[0]} if len(options) == 1 else None
        first, last = values[0]["longitude_deg"], values[-1]["longitude_deg"]
        chart["longitude_ranges_deg"][key] = {
            "start": first, "end": last, "crosses_zero": last < first,
        }
        if len(options) > 1:
            omissions.append({"field": f"natal.{key}",
                              "reason": f"출생일 안에서 {label} 별자리가 바뀌어 해당 요소는 제외했어요."})
    return chart, omissions


def build_profile(payload):
    """Calculate a chart and obtain five 1..10 suggestions from astro_policy.

    Gregorian birth dates are supported from 1900 through today. Unknown birth
    times use the whole valid local day, including both DST folds; a date still
    in progress ends at the current instant, and a future local day is rejected.
    Hour pillars
    are omitted; year/month/day and Sun/Moon signs are used only when stable.
    Sun and Moon move less than a sign between the hourly samples in this date
    range; endpoint samples include the first and last valid civil second.
    """
    if not isinstance(payload, dict):
        raise AstroProfileError("출생 정보를 JSON 객체로 입력해 주세요.")
    allowed = {"birth_date", "birth_time", "birth_timezone", "birth_fold", "target_date", "target_timezone"}
    if set(payload) - allowed:
        raise AstroProfileError("지원하지 않는 출생 정보 항목이 있어요.")
    if not {"birth_date", "birth_timezone", "target_date"} <= set(payload):
        raise AstroProfileError("생년월일, 출생지 시간대와 분석 날짜를 입력해 주세요.")
    if payload.get("target_timezone", TARGET_TIMEZONE) != TARGET_TIMEZONE:
        raise AstroProfileError("이 지도는 뉴욕 시간대를 기준으로 분석해요.")
    fold = payload.get("birth_fold")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise AstroProfileError("중복 출생 시간은 첫 번째 또는 두 번째를 선택해 주세요.")
    now = datetime.now(_UTC)
    zone = _timezone_value(payload["birth_timezone"])
    day = _date_value(payload["birth_date"], birth=True, now=now)
    target_day = _date_value(payload["target_date"], birth=False, now=now)
    clock_text = payload.get("birth_time")
    if clock_text is None:
        natal, omissions = _unknown_natal(day, zone, now)
    else:
        local = _known_local(day, clock_text, zone, fold)
        if local.astimezone(_UTC) > now:
            raise AstroProfileError("출생 시간은 현재보다 이전이어야 해요.")
        natal, omissions = _known_natal(local)
    target_local = datetime.combine(target_day, time(12), ZoneInfo(TARGET_TIMEZONE))
    today = _chart_at(target_local)
    today.update(date=target_day.isoformat(), timezone=TARGET_TIMEZONE,
                 reference_local=target_local.isoformat(), reference_label="뉴욕 현지 시각 정오")

    from .astro_policy import interpret_profile
    interpreted = dict(interpret_profile(natal, today))
    policy_omissions = interpreted.pop("omissions", [])
    omitted_fields = {item["field"] for item in omissions}
    for item in policy_omissions:
        if item["field"] not in omitted_fields:
            omissions.append(item)
            omitted_fields.add(item["field"])
    return {
        "schema_version": 1, "profile_version": PROFILE_VERSION,
        "conventions": {
            "calendar": "gregorian", "saju_sect": 2,
            "saju_year_month": "절기 시각은 고정 UTC+08:00으로 변환한 같은 순간으로 비교",
            "saju_day_hour": "출생지 민간시각의 일주·시주; sect 2는 23시대 일주를 당일로 처리",
            "day_boundary": "00:00; 시주의 천간은 lunar-python의 야자시 규칙을 따름",
            "mixed_calendar_convention": True, "true_solar_time": False,
            "hidden_stems": False, "yongshin": False,
            "zodiac": "tropical-geocentric-true-ecliptic-of-date",
            "daily_reference": "12:00 America/New_York",
            "unknown_time": "현지 날짜 전체에서 유지되는 요소만 사용; 오늘 출생은 현재까지만 비교; 시주·변하는 요소 생략",
            "libraries": {"lunar-python": version("lunar_python"),
                          "astronomy-engine": version("astronomy-engine")},
        },
        "natal": natal, "today": today, "omissions": omissions, **interpreted,
    }
