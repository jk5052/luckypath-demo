"""Independent astrology facts and an explicitly experimental walking adapter.

Kerykeion owns celestial/chart calculations; the small, inspectable policy below
is application design. It is not an upstream astrology score or a validated
measure. No full upstream subject (which includes private birth data) is emitted.
"""
from __future__ import annotations

from datetime import timezone
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
import math
import logging
import os
from pathlib import Path
from threading import RLock


ANALYSIS_VERSION = "kerykeion-natal-transits-v1"
POLICY_ID = "astrology-walking-experimental-v1"
KEYS = ("open", "green", "still", "social", "access")
PLANETS = ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto")
PERSONAL = PLANETS[:5]
LABELS = dict(zip(PLANETS, ("태양", "달", "수성", "금성", "화성", "목성", "토성", "천왕성", "해왕성", "명왕성")))
SIGNS = ("양자리", "황소자리", "쌍둥이자리", "게자리", "사자자리", "처녀자리",
         "천칭자리", "전갈자리", "사수자리", "염소자리", "물병자리", "물고기자리")
ASPECT_LABELS = {"conjunction": "합", "sextile": "육분", "square": "사각", "trine": "삼분", "opposition": "충"}
NATAL_ORBS = {"conjunction": 6., "sextile": 4., "square": 6., "trine": 6., "opposition": 6.}
TRANSIT_ORBS = {key: 3. for key in NATAL_ORBS}
POLICY_ORB = 3.
# App-authored themes, tuple order: open, green, still, social, access.
PLANET_VECTORS = {
    "Sun": (8, 5, 4, 7, 6), "Moon": (4, 8, 9, 4, 6),
    "Mercury": (8, 4, 4, 8, 8), "Venus": (5, 8, 5, 9, 6),
    "Mars": (9, 5, 2, 6, 5), "Jupiter": (10, 7, 4, 8, 6),
    "Saturn": (4, 5, 9, 3, 9), "Uranus": (10, 4, 3, 7, 4),
    "Neptune": (5, 9, 9, 4, 4), "Pluto": (5, 6, 9, 3, 5),
}
ELEMENT_VECTORS = {
    "Fire": (9, 4, 3, 8, 5), "Earth": (5, 9, 7, 5, 9),
    "Air": (9, 4, 4, 9, 7), "Water": (4, 8, 9, 4, 6),
}
_HOUSE_NAMES = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth")
_LOCK = RLock()


def _backend():
    # Configure BEFORE importing the provider. No personal data is cached here;
    # any provider data directory stays inside the project workspace.
    os.environ.update(
        KERYKEION_BACKEND="libephemeris", KERYKEION_LEB_MODE="leb",
        LIBEPHEMERIS_NETWORK_POLICY="sealed", LIBEPHEMERIS_PRECISION="base",
        LIBEPHEMERIS_DATA_DIR=str(Path(__file__).resolve().parents[1] / "work" / "ephemeris"),
    )
    from kerykeion import AstrologicalSubjectFactory, AspectsFactory, BACKEND_NAME
    import libephemeris
    from libephemeris.logging_config import disable_logging
    disable_logging()
    # Provider DEBUG/warnings can include timestamps or coordinates. Keep only
    # our sanitized structured facts/errors, including polar-fallback notices.
    package_logger = logging.getLogger("kerykeion")
    package_logger.handlers = [logging.NullHandler()]
    package_logger.propagate = False
    if BACKEND_NAME != "libephemeris":
        raise ValueError("별자리 계산 엔진 설정을 확인해 주세요.")
    libephemeris.set_calc_mode("leb")
    libephemeris.set_network_policy("sealed")
    libephemeris.set_precision_tier("base")
    return AstrologicalSubjectFactory, AspectsFactory, libephemeris


@lru_cache(maxsize=2)
def _data_metadata(filename):
    # Cache only the immutable ephemeris's digest, never a chart or a birth date.
    path = Path(filename)
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"artifact": path.name, "sha256": digest.hexdigest(),
            "origin": "NASA/JPL DE440s", "representation": "libephemeris reviewed LEB2 core",
            "coverage": "1850–2150 (upper boundary exclusive)",
            "source_url": "https://ssd.jpl.nasa.gov/doc/de440_de441.html"}


def _subject(factory, moment, *, latitude=None, longitude=None, house_system="P", name="chart"):
    located = latitude is not None and longitude is not None
    # The provider requires coordinates even for purely geocentric positions.
    # (0,0) is an internal neutral computation frame, never a presumed birthplace.
    # All products depending on it are stripped; dignities needing sect are off.
    return factory.from_iso_utc_time(
        name=name, iso_utc_time=moment.astimezone(timezone.utc).isoformat(),
        tz_str="UTC", online=False,
        lng=longitude if located else 0., lat=latitude if located else 0.,
        zodiac_type="Tropical", perspective_type="Apparent Geocentric",
        houses_system_identifier=house_system,
        active_points=list(PLANETS) + (["Ascendant", "Medium_Coeli"] if located else []),
        calculate_lunar_phase=True, calculate_dignities=located,
        suppress_geonames_warning=True,
    )


def _point(point, *, located=False):
    return {
        "name": point.name, "label": LABELS.get(point.name, point.name),
        "longitude_deg": point.abs_pos, "sign_index": point.sign_num,
        "sign": SIGNS[point.sign_num], "element": point.element, "quality": point.quality,
        "speed_deg_per_day": point.speed, "retrograde": point.retrograde,
        "motion_state": point.motion_state, "house": point.house if located else None,
        "essential_dignity": point.essential_dignity if located else None,
        "dignity_score": point.dignity_score if located else None,
        "source": point.source, "source_reviewed": point.source_reviewed,
    }


def _chart(subject, *, located=False):
    planets = {name: _point(getattr(subject, name.lower()), located=located) for name in PLANETS}
    if any(point["source"] != "LEB" or point["source_reviewed"] is not True for point in planets.values()):
        raise ValueError("검증된 로컬 천체 데이터가 없어 별자리 분석을 중단했어요.")
    houses = angles = None
    if located:
        houses = [{"number": i + 1, "longitude_deg": getattr(subject, f"{name}_house").abs_pos}
                  for i, name in enumerate(_HOUSE_NAMES)]
        angles = {"ascendant": {"longitude_deg": subject.ascendant.abs_pos,
                                "sign": SIGNS[subject.ascendant.sign_num]},
                  "midheaven": {"longitude_deg": subject.medium_coeli.abs_pos,
                                "sign": SIGNS[subject.medium_coeli.sign_num]}}
    # Do not emit the upstream fallback model: it includes the raw latitude.
    fallback = [{"requested": item.requested_house_system_identifier,
                 "used": item.used_house_system_identifier, "strategy": item.strategy}
                for item in subject.polar_house_fallbacks] if located else []
    return {"planets": planets, "houses": houses, "angles": angles,
            "house_system": subject.effective_houses_system_identifier if located else None,
            "house_fallbacks": fallback}


def _aspect(item):
    return {"first": item.p1_name, "second": item.p2_name,
            "aspect": item.aspect, "label": ASPECT_LABELS[item.aspect],
            "angle_deg": item.aspect_degrees, "orb_deg": abs(item.orbit),
            "movement": item.aspect_movement}


def _aspects(factory, first, second=None):
    kwargs = {"active_points": list(PLANETS), "point_orb_adjustments": {},
              "active_aspects": [{"name": name, "orb": orb}
                                 for name, orb in (TRANSIT_ORBS if second else NATAL_ORBS).items()]}
    if second is None:
        items = factory.single_chart_aspects(first, **kwargs).aspects
    else:
        # first = moving sky, second = fixed natal chart; speeds must reflect it.
        items = factory.dual_chart_aspects(first, second, second_subject_is_fixed=True, **kwargs).aspects
    return sorted((_aspect(item) for item in items), key=lambda x: (x["orb_deg"], x["first"], x["second"]))


def _unknown_chart(subjects):
    charts = [_chart(subject) for subject in subjects]
    planets, omissions = {}, []
    for name in PLANETS:
        options = [chart["planets"][name] for chart in charts]
        indices = list(dict.fromkeys(point["sign_index"] for point in options))
        directions = set(point["retrograde"] for point in options)
        first, last = options[0]["longitude_deg"], options[-1]["longitude_deg"]
        # Keep the range explicit; none of these values is a fabricated noon.
        unwrapped = [first]
        for point in options[1:]:
            delta = (point["longitude_deg"] - unwrapped[-1] + 180) % 360 - 180
            unwrapped.append(unwrapped[-1] + delta)
        point = dict(options[0])
        point.update(longitude_deg=None, speed_deg_per_day=None, motion_state=None,
                     sign=SIGNS[indices[0]] if len(indices) == 1 else None,
                     sign_index=indices[0] if len(indices) == 1 else None,
                     element=options[0]["element"] if len(indices) == 1 else None,
                     quality=options[0]["quality"] if len(indices) == 1 else None,
                     retrograde=options[0]["retrograde"] if len(directions) == 1 else None,
                     alternatives=[SIGNS[index] for index in indices],
                     longitude_range_deg={"start": first, "end": last,
                                          "min_unwrapped": min(unwrapped), "max_unwrapped": max(unwrapped)})
        planets[name] = point
        if len(indices) > 1:
            omissions.append({"field": f"natal.{name}.sign", "reason": f"출생일 중 {LABELS[name]} 별자리가 바뀌어 점수에서 제외했어요."})
        if len(directions) > 1:
            omissions.append({"field": f"natal.{name}.retrograde", "reason": f"출생일 중 {LABELS[name]}의 순행·역행이 바뀌어 확정하지 않았어요."})
    return {"planets": planets, "houses": None, "angles": None, "house_system": None,
            "house_fallbacks": []}, omissions


def _vector(values):
    return dict(zip(KEYS, values))


def _mean_vectors(*vectors):
    return [sum(items) / len(vectors) for items in zip(*vectors)]


def _aspect_vector(aspect, *, retrograde=False):
    values = _mean_vectors(PLANET_VECTORS[aspect["first"]], PLANET_VECTORS[aspect["second"]])
    if aspect["aspect"] in ("square", "opposition"):
        values[0] -= 1
        values[2] += 2
        values[3] -= 1
    elif aspect["aspect"] in ("sextile", "trine"):
        values[0] += 1
        values[3] += 1
    if retrograde:
        values[2] += 1
        values[4] += 1
    return _vector([max(1., min(10., value)) for value in values])


def _profile(natal, today, natal_aspects, transits):
    evidence = []
    for name in PERSONAL:
        point = natal["planets"][name]
        if point["element"] is not None:
            evidence.append({"id": f"natal-{name}", "label": f"출생 {LABELS[name]} 배치",
                             "fact": f"{LABELS[name]} · {point['sign']}", "rule_id": "personal-placement",
                             "vector": _vector(_mean_vectors(PLANET_VECTORS[name], ELEMENT_VECTORS[point["element"]])),
                             "weight": 1.})
    moon = today["planets"]["Moon"]
    evidence.append({"id": "today-Moon", "label": "오늘의 달 배치", "fact": f"달 · {moon['sign']}",
                     "rule_id": "daily-moon-placement",
                     "vector": _vector(_mean_vectors(PLANET_VECTORS["Moon"], ELEMENT_VECTORS[moon["element"]])),
                     "weight": 1.})
    for family, aspects in (("natal", natal_aspects), ("transit", transits)):
        for aspect in aspects:
            if aspect["orb_deg"] >= POLICY_ORB:
                continue
            if family == "transit" and aspect["second"] not in PERSONAL:
                continue
            if family == "natal" and not ({aspect["first"], aspect["second"]} & set(PERSONAL)):
                continue
            moving = today if family == "transit" else natal
            checked_names = [aspect["first"]] if family == "transit" else [aspect["first"], aspect["second"]]
            retrograde_names = [name for name in checked_names if moving["planets"][name]["retrograde"] is True]
            retrograde = bool(retrograde_names)
            first, second = LABELS[aspect["first"]], LABELS[aspect["second"]]
            fact = f"{first}–{second} {aspect['label']} · 오브 {aspect['orb_deg']:.2f}° · {aspect['movement']}"
            if retrograde:
                fact += " · " + "·".join(LABELS[name] for name in retrograde_names) + " 역행"
            evidence.append({"id": f"{family}-{aspect['first']}-{aspect['second']}-{aspect['aspect']}",
                             "label": "오늘→출생 트랜싯" if family == "transit" else "출생 주요 각",
                             "fact": fact, "rule_id": f"{family}-tight-major-aspect",
                             "vector": _aspect_vector(aspect, retrograde=retrograde),
                             "weight": 1 - aspect["orb_deg"] / POLICY_ORB,
                             "source_fact": {**aspect, "retrograde_planets": retrograde_names}})
    total = sum(item["weight"] for item in evidence)
    details = {}
    for key in KEYS:
        value = sum(item["vector"][key] * item["weight"] for item in evidence) / total
        details[key] = {"unrounded": value, "rounded": math.floor(value + .5)}
    return {"status": "ready", "policy_id": POLICY_ID,
            "profile_scores": {key: item["rounded"] for key, item in details.items()},
            "score_details": details, "evidence": evidence,
            "policy": {"id": POLICY_ID, "experimental": True, "externally_validated": False,
                       "label": "별자리 분석을 산책 중요도로 옮기는 실험 규칙",
                       "score_keys": list(KEYS), "personal_planets": list(PERSONAL),
                       "planet_vectors": {key: _vector(value) for key, value in PLANET_VECTORS.items()},
                       "element_vectors": {key: _vector(value) for key, value in ELEMENT_VECTORS.items()},
                       "placement_rule": "출생 개인 천체와 오늘 달: 천체 벡터·별자리 원소 벡터의 평균, 항목당 비중 1",
                       "aspect_rule": "3° 미만 주요 각: 두 천체 벡터 평균, 비중 1−오브/3; 개인 천체 관련 각만 반영",
                       "aspect_adjustments": {"square/opposition": {"open": -1, "still": 2, "social": -1},
                                              "sextile/trine": {"open": 1, "social": 1},
                                              "conjunction": {}},
                       "retrograde_adjustment": {"still": 1, "access": 1},
                       "retrograde_scope": "출생 각은 두 천체 중 하나라도 역행이면 한 번 적용; 트랜싯은 오늘 움직이는 천체만 확인",
                       "aggregation": "각 벡터를 1..10으로 제한 후 근거별 비중 평균; 최종 half-up 정수 반올림",
                       "not_used_for_weights": ["houses", "angles", "dignity_score", "aspect_movement"],
                       "not_an_upstream_score": True}}


def analyze_astrology(context):
    """Use an analysis_input.prepare_input context; never call the saju engine."""
    with _LOCK:
        return _analyze(context)


def _analyze(context):
    factory, aspect_factory, ephe = _backend()
    known = context["birth_local"] is not None
    located = known and context.get("latitude") is not None and context.get("longitude") is not None
    house_system = context.get("house_system", "P")
    omissions = []
    try:
        if known:
            natal_subject = _subject(factory, context["birth_local"],
                                     latitude=context.get("latitude"), longitude=context.get("longitude"),
                                     house_system=house_system, name="natal")
            natal = _chart(natal_subject, located=located)
            natal_aspects = _aspects(aspect_factory, natal_subject)
        else:
            subjects = [_subject(factory, instant, name="natal") for instant in context["birth_samples"]]
            natal, missing = _unknown_chart(subjects)
            omissions.extend(missing)
            natal_subject, natal_aspects = None, []
            omissions.append({"field": "natal.aspects/transits", "reason": "출생 시간을 몰라 정확한 오브와 접근·분리 여부를 확정할 수 없어 출생 각과 개인 트랜싯은 생략했어요."})
        # Today's geocentric sky needs no invented observation location.
        today_subject = _subject(factory, context["target_local"], name="today")
        today = _chart(today_subject)
        today.update(date=context["target_local"].date().isoformat(), timezone="America/New_York",
                     reference_local=context["target_local"].isoformat(), reference_label="뉴욕 현지 정오")
        transits = _aspects(aspect_factory, today_subject, natal_subject) if known else []
        today_aspects = _aspects(aspect_factory, today_subject)
    except Exception:
        # Provider errors can contain the input timestamp; keep API errors generic.
        raise ValueError("로컬 별자리 계산을 완료하지 못했어요. 입력 범위와 천체 데이터 설정을 확인해 주세요.") from None
    if not located:
        omissions.append({"field": "natal.houses/angles/dignities",
                          "reason": "정확한 출생 시간과 출생지 좌표가 모두 있어야 하우스·상승점·위치에 따른 위계를 계산해요."})
    for fallback in natal["house_fallbacks"]:
        omissions.append({"field": "natal.house_system", "reason": f"해당 위도에서는 요청한 하우스 계산이 성립하지 않아 {fallback['used']} 방식으로 대체했어요."})
    natal["time_known"] = known
    natal["birth_window"] = dict(context["birth_window"])
    natal["aspects"] = natal_aspects
    today["aspects"] = today_aspects
    filename, _, _, _ = ephe.get_current_file_data(0)
    if not filename or ephe.get_calc_mode() != "leb" or ephe.get_network_policy() != "sealed":
        raise ValueError("별자리 계산 데이터의 출처를 확인하지 못했어요.")
    data = _data_metadata(filename)
    provider = {"name": "Kerykeion", "version": version("kerykeion"),
                "backend": "libephemeris", "backend_version": version("libephemeris"),
                "mode": ephe.get_calc_mode(), "network_policy": ephe.get_network_policy(),
                "license": "AGPL-3.0", "use_context": "local prototype; deployment license decision pending",
                "data": data, "zodiac": "Tropical", "perspective": "Apparent Geocentric",
                "natal_aspect_orbs_deg": dict(NATAL_ORBS), "transit_aspect_orbs_deg": dict(TRANSIT_ORBS),
                "luminary_orb_adjustments": {},
                "nasa_role": "천체 위치 데이터 출처; 해석이나 산책 가중치의 검증·인증을 의미하지 않음"}
    highlights = [
        {"label": "출생 태양", "value": natal["planets"]["Sun"]["sign"] or "시간에 따라 달라짐"},
        {"label": "출생 달", "value": natal["planets"]["Moon"]["sign"] or "시간에 따라 달라짐"},
        {"label": "출생 주요 각", "value": f"{len(natal_aspects)}개" if known else "시간 미상으로 생략"},
        {"label": "오늘의 개인 트랜싯", "value": f"{len(transits)}개 · 오브 3°" if known else "시간 미상으로 생략"},
        {"label": "오늘의 달", "value": today["planets"]["Moon"]["sign"]},
        {"label": "하우스", "value": natal["house_system"] or "출생 시간·좌표 필요"},
    ]
    planet_rows = [[LABELS[name], point["sign"] or " / ".join(point.get("alternatives", [])),
                    f"{point['longitude_deg']:.3f}°" if point["longitude_deg"] is not None else "범위로 계산",
                    "역행" if point["retrograde"] is True else "순행" if point["retrograde"] is False else "미확정",
                    point["house"] or "—"] for name, point in natal["planets"].items()]
    transit_rows = [[LABELS[item["first"]], LABELS[item["second"]], item["label"],
                     f"{item['orb_deg']:.3f}°", item["movement"]] for item in transits]
    result = {"system": "astrology", "analysis_version": ANALYSIS_VERSION, "provider": provider,
              "highlights": highlights,
              "display_sections": [{"title": "출생 천체", "columns": ["천체", "별자리", "황경", "운동", "하우스"], "rows": planet_rows},
                                   {"title": "오늘→출생 트랜싯", "columns": ["오늘", "출생", "각", "오브", "움직임"], "rows": transit_rows}],
              "analysis": {"natal": natal, "today": today, "transits": transits,
                           "transit_direction": "today moving → natal fixed", "requested_house_system": house_system},
              "omissions": omissions, "profile": _profile(natal, today, natal_aspects, transits)}
    _check_finite(result)
    return result


def _check_finite(value):
    if isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("별자리 계산에 유효하지 않은 수치가 있어요.")
