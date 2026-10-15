"""Independent saju structure and a separately identified walking policy.

Calendar facts and ten-god relations follow lunar-python. Walking themes,
vectors, and averaging are experimental product rules, not validated
predictions or a measurement of element strength. No astrology is imported.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from importlib.metadata import version
from itertools import combinations

from lunar_python import EightChar, Solar
from lunar_python.util import LunarUtil

from .scoring import WEIGHT_KEYS


ANALYSIS_VERSION = "saju-structure-v1"
POLICY_ID = "saju-ten-god-walk-v1"
SOURCE = "https://github.com/6tail/lunar-python"
_UTC8 = timezone(timedelta(hours=8))
_PILLARS = ("year", "month", "day", "hour")
_PILLAR_KO = {"year": "연주", "month": "월주", "day": "일주", "hour": "시주"}
_ELEMENTS = {"木": "wood", "火": "fire", "土": "earth", "金": "metal", "水": "water"}
_ELEMENT_KO = {"wood": "목", "fire": "화", "earth": "토", "metal": "금", "water": "수"}
_STEM_KO = dict(zip("甲乙丙丁戊己庚辛壬癸", "갑을병정무기경신임계"))
_BRANCH_KO = dict(zip("子丑寅卯辰巳午未申酉戌亥", "자축인묘진사오미신유술해"))
_TEN_GODS = {
    "比肩": ("peer_same", "비견", "peer", "일간과 같은 오행·같은 음양"),
    "劫财": ("peer_opposite", "겁재", "peer", "일간과 같은 오행·다른 음양"),
    "食神": ("expression_same", "식신", "expression", "일간이 생하는 오행·같은 음양"),
    "伤官": ("expression_opposite", "상관", "expression", "일간이 생하는 오행·다른 음양"),
    "偏财": ("wealth_same", "편재", "wealth", "일간이 극하는 오행·같은 음양"),
    "正财": ("wealth_opposite", "정재", "wealth", "일간이 극하는 오행·다른 음양"),
    "七杀": ("authority_same", "편관", "authority", "일간을 극하는 오행·같은 음양"),
    "正官": ("authority_opposite", "정관", "authority", "일간을 극하는 오행·다른 음양"),
    "偏印": ("resource_same", "편인", "resource", "일간을 생하는 오행·같은 음양"),
    "正印": ("resource_opposite", "정인", "resource", "일간을 생하는 오행·다른 음양"),
}
# Same offsets and direction rule as lunar-python 1.4.8 EightChar.__getDiShi.
# Applying the rule to explicit stems avoids using a UTC+8 date's day master
# for a birthplace-civil day pillar near midnight.
_STAGE_OFFSETS = dict(zip("甲乙丙丁戊己庚辛壬癸", (1, 6, 10, 9, 10, 9, 7, 0, 4, 3)))
_STAGE_KO = dict(zip(EightChar.CHANG_SHENG,
                    ("장생", "목욕", "관대", "건록", "제왕", "쇠", "병", "사", "묘", "절", "태", "양")))
_THEMES = {
    "peer": {"label": "비겁", "focus": ("social",),
             "rationale": "같은 오행의 관계를 사람과 연결하는 활동이라는 제품 주제로 연결"},
    "expression": {"label": "식상", "focus": ("open", "social"),
                   "rationale": "일간이 생하는 관계를 표현·활동이라는 제품 주제로 연결"},
    "wealth": {"label": "재성", "focus": ("access",),
               "rationale": "일간이 극하는 관계를 실용적인 활동·편의라는 제품 주제로 연결"},
    "authority": {"label": "관성", "focus": ("still", "access"),
                  "rationale": "일간을 극하는 관계를 질서·집중이라는 제품 주제로 연결"},
    "resource": {"label": "인성", "focus": ("still", "green"),
                 "rationale": "일간을 생하는 관계를 휴식·돌봄이라는 제품 주제로 연결"},
}


def _stem(stem):
    element = _ELEMENTS[LunarUtil.WU_XING_GAN[stem]]
    index = LunarUtil.GAN.index(stem) - 1
    return {"text": stem, "korean": _STEM_KO[stem], "element": element,
            "element_korean": _ELEMENT_KO[element], "yin_yang": "양" if index % 2 == 0 else "음"}


def _ten_god(day_master, stem):
    if day_master is None:
        return None
    chinese = LunarUtil.SHI_SHEN[day_master + stem]
    key, korean, group, relation = _TEN_GODS[chinese]
    return {"key": key, "chinese": chinese, "korean": korean,
            "group": group, "group_korean": _THEMES[group]["label"], "relation": relation}


def _stage(day_master, branch):
    if day_master is None:
        return None
    branch_index = LunarUtil.ZHI.index(branch) - 1
    yang = (LunarUtil.GAN.index(day_master) - 1) % 2 == 0
    name = EightChar.CHANG_SHENG[(_STAGE_OFFSETS[day_master] + (branch_index if yang else -branch_index)) % 12]
    return {"chinese": name, "korean": _STAGE_KO[name], "relative_to": day_master}


def _plain_pillar(text):
    return {"text": text, "korean": _STEM_KO[text[0]] + _BRANCH_KO[text[1]],
            "stem": text[0], "branch": text[1]}


def _term(term):
    solar = term.getSolar()
    return {"name": term.getName(), "time_utc8": solar.toYmdHms(), "timezone": "UTC+08:00"}


def _chart_at(local):
    if not isinstance(local, datetime) or local.utcoffset() is None:
        raise ValueError("사주 계산에는 시간대가 있는 출생·분석 시각이 필요합니다.")
    boundary_lunar = Solar.fromDate(local.astimezone(_UTC8)).getLunar()
    boundary = boundary_lunar.getEightChar()
    civil = Solar.fromDate(local).getLunar().getEightChar()
    civil.setSect(2)
    return {
        "pillars": dict(zip(_PILLARS, map(_plain_pillar,
                     (boundary.getYear(), boundary.getMonth(), civil.getDay(), civil.getTime())))),
        "month_terms": {"previous_jie": _term(boundary_lunar.getPrevJie()),
                        "next_jie": _term(boundary_lunar.getNextJie())},
    }


def _enrich(pillar, day_master, *, is_natal_day=False):
    if pillar is None:
        return None
    result = dict(pillar)
    result["stem_info"] = _stem(pillar["stem"])
    result["stem_ten_god"] = None if is_natal_day else _ten_god(day_master, pillar["stem"])
    result["is_day_master"] = is_natal_day
    result["branch_element"] = _ELEMENTS[LunarUtil.WU_XING_ZHI[pillar["branch"]]]
    result["hidden_stems"] = [
        {**_stem(stem), "order_in_library": index + 1, "is_main_qi": index == 0,
         "ten_god": _ten_god(day_master, stem)}
        for index, stem in enumerate(LunarUtil.ZHI_HIDE_GAN[pillar["branch"]])
    ]
    result["twelve_stage"] = _stage(day_master, pillar["branch"])
    return result


def _root_evidence(pillars, day_master):
    if day_master is None:
        return []
    element = _ELEMENTS[LunarUtil.WU_XING_GAN[day_master]]
    return [{"pillar": key, "pillar_label": _PILLAR_KO[key], "branch": pillar["branch"],
             "hidden_stem": hidden["text"], "order_in_library": hidden["order_in_library"],
             "is_main_qi": hidden["is_main_qi"], "same_stem": hidden["text"] == day_master,
             "same_element": True,
             "fact": f"{_PILLAR_KO[key]} {pillar['branch']}의 지장간 {hidden['text']}이 일간 {day_master}와 같은 오행"}
            for key, pillar in pillars.items() if pillar is not None
            for hidden in pillar["hidden_stems"] if hidden["element"] == element]


def _relations(pillars):
    """List visible pair relations; do not assert a transformation occurred."""
    result = []
    for (left_key, left), (right_key, right) in combinations(
            [(key, value) for key, value in pillars.items() if value is not None], 2):
        positions = [left_key, right_key]
        stem_index = LunarUtil.GAN.index(left["stem"]) - 1
        branch_index = LunarUtil.ZHI.index(left["branch"]) - 1
        for condition, kind, label, characters in (
            (LunarUtil.HE_GAN_5[stem_index] == right["stem"], "stem_combine", "천간합", left["stem"] + right["stem"]),
            (LunarUtil.HE_ZHI_6[branch_index] == right["branch"], "branch_combine", "지지육합", left["branch"] + right["branch"]),
            (LunarUtil.ZHI[(branch_index + 6) % 12 + 1] == right["branch"], "branch_clash", "지지충", left["branch"] + right["branch"]),
        ):
            if condition:
                result.append({"kind": kind, "label": label, "positions": positions,
                               "characters": characters, "transformation_assessed": False})
    return result


def _month_context(pillar, terms):
    if pillar is None:
        return None
    branch = pillar["branch"]
    seasons = (("寅卯辰", "봄"), ("巳午未", "여름"), ("申酉戌", "가을"), ("亥子丑", "겨울"))
    season = next(name for branches, name in seasons if branch in branches)
    return {"month_branch": branch, "season": season,
            "meaning": "월지의 전통적 계절 분류이며 출생지의 실제 날씨나 세력 점수가 아닙니다.",
            **terms}


def policy_definition():
    return {
        "id": POLICY_ID, "kind": "experimental_product_mapping", "externally_validated": False,
        "description": "일간 기준 십신 관계를 산책 주제로 연결하는 실험 규칙",
        "base_value": 3, "emphasized_value": 9,
        "themes": {key: {"label": value["label"], "focus": list(value["focus"]),
                          "rationale": value["rationale"],
                          "vector": {axis: 9 if axis in value["focus"] else 3 for axis in WEIGHT_KEYS}}
                   for key, value in _THEMES.items()},
        "component_weights": {"natal": .5, "today": .5},
        "evidence_weights": (
            "원국은 일간 자체를 제외한 천간마다 1, 사용 가능한 지지마다 총 1을 부여합니다. "
            "지지 안의 지장간은 같은 비중으로 나눕니다. 오늘은 일주 천간 1·지지 총 1을 사용합니다. "
            "각 묶음 안에서 합이 1이 되게 한 뒤 원국·오늘을 절반씩 반영합니다."
        ),
        "weight_limits": "지장간의 같은 비중은 제품의 단순화이며 명리의 세력·통근 강도·용신 가중치가 아닙니다.",
        "missing_policy": "빠진 근거는 제외해 다시 정규화합니다. 일간이 확정되지 않으면 관계 점수를 만들지 않습니다.",
        "rounding": "exact rational mean; nearest integer with .5 rounded up",
        "score_meaning": "1은 기준 제외, 10은 높은 중요도입니다. 환경 필요량·예측 확률이 아닙니다.",
        "route_targets": {"open": .8, "social": .8, "access": .8},
        "excluded_from_profile": ["신강·신약", "용신·희신", "대운", "합화 성립 판단", "12운성의 길흉 점수", "통근 강도"],
    }


def _make_profile(natal, today, day_master):
    policy = policy_definition()
    if day_master is None:
        return {"status": "unavailable", "policy_id": POLICY_ID, "profile_scores": None,
                "score_details": {}, "evidence": [], "policy": policy,
                "reason": "일간이 확정되지 않아 십신 관계를 산책 점수로 바꿀 수 없습니다."}
    evidence = []

    def add(component, position, target_stem, god, raw_weight, label):
        if god is None:
            return
        group = god["group"]
        evidence.append({"id": f"saju.{component}.{position}", "component": component,
                         "label": label,
                         "fact": f"일간 {day_master} 기준 {target_stem}은 {god['korean']}({god['chinese']}): {god['relation']}",
                         "ten_god": god["korean"], "theme": group,
                         "rule_id": f"{POLICY_ID}.{group}",
                         "vector": dict(policy["themes"][group]["vector"]),
                         "_raw_weight": raw_weight})

    for component, pillars in (("natal", natal["pillars"]), ("today", {"day": today["pillars"]["day"]})):
        for key, pillar in pillars.items():
            if pillar is None:
                continue
            prefix = "원국" if component == "natal" else "오늘"
            if not (component == "natal" and key == "day"):
                add(component, f"{key}.stem", pillar["stem"], pillar["stem_ten_god"], Fraction(1),
                    f"{prefix} {_PILLAR_KO[key]} 천간 {pillar['stem']}")
            hidden_count = len(pillar["hidden_stems"])
            for hidden in pillar["hidden_stems"]:
                add(component, f"{key}.hidden.{hidden['order_in_library']}", hidden["text"],
                    hidden["ten_god"], Fraction(1, hidden_count),
                    f"{prefix} {_PILLAR_KO[key]} {pillar['branch']}의 지장간 {hidden['text']}")
    totals = {component: sum((item["_raw_weight"] for item in evidence if item["component"] == component), Fraction(0))
              for component in ("natal", "today")}
    available = [component for component, total in totals.items() if total]
    if not available:
        return {"status": "unavailable", "policy_id": POLICY_ID, "profile_scores": None,
                "score_details": {}, "evidence": [], "policy": policy, "reason": "사용 가능한 십신 관계가 없습니다."}
    values = dict.fromkeys(WEIGHT_KEYS, Fraction(0))
    for item in evidence:
        raw_weight = item.pop("_raw_weight")
        weight = raw_weight / totals[item["component"]] / len(available)
        item["raw_weight"] = float(raw_weight)
        item["weight"] = float(weight)
        for axis in WEIGHT_KEYS:
            values[axis] += weight * item["vector"][axis]
    scores = {key: (2 * value.numerator + value.denominator) // (2 * value.denominator)
              for key, value in values.items()}
    policy["effective_component_weights"] = {key: 1 / len(available) if key in available else 0
                                              for key in ("natal", "today")}
    policy["missing_components"] = [key for key in totals if key not in available]
    return {"status": "ready", "policy_id": POLICY_ID, "profile_scores": scores,
            "score_details": {key: {"unrounded": float(value), "rounded": scores[key]} for key, value in values.items()},
            "evidence": evidence, "policy": policy}


def _display_sections(natal, today):
    sections = []
    for title, chart in (("사주 원국", natal), ("오늘의 관계 · 출생 일간 기준", today)):
        rows = []
        for key, pillar in chart["pillars"].items():
            if pillar is None:
                rows.append([_PILLAR_KO[key], "미확정·생략", "—", "—", "—"])
                continue
            god = pillar["stem_ten_god"]
            hidden = ", ".join(f"{h['text']} {h['ten_god']['korean'] if h['ten_god'] else '관계 미확정'}"
                               for h in pillar["hidden_stems"])
            stage = pillar["twelve_stage"]
            rows.append([_PILLAR_KO[key], f"{pillar['text']} · {pillar['korean']}",
                         "일간" if pillar["is_day_master"] else god["korean"] if god else "미확정",
                         hidden, stage["korean"] if stage else "미확정"])
        sections.append({"title": title, "columns": ["기둥", "간지", "천간 십신", "지장간과 십신", "12운성"], "rows": rows})
    return sections


def analyze_saju(context):
    """Read the shared validated time context and return only saju results."""
    samples = context.get("birth_samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("출생 시각 범위가 필요합니다.")
    known = context.get("birth_local") is not None
    charts = [_chart_at(instant) for instant in samples]
    raw_today = _chart_at(context["target_local"])
    stable, alternatives, omissions = {}, {}, []
    for key in _PILLARS:
        options = {chart["pillars"][key]["text"]: chart["pillars"][key] for chart in charts}
        alternatives[key] = list(options.values()) if key != "hour" or known else []
        stable[key] = next(iter(options.values())) if len(options) == 1 and (known or key != "hour") else None
        if stable[key] is None:
            reason = "출생 시간을 몰라 시주를 제외했습니다." if key == "hour" and not known else f"출생 시각 범위 안에서 {_PILLAR_KO[key]}가 바뀌어 제외했습니다."
            omissions.append({"field": f"saju.natal.{key}", "reason": reason})
    day_master = stable["day"]["stem"] if stable["day"] else None
    if day_master is None:
        omissions.append({"field": "saju.day_master_relations", "reason": "일간이 확정되지 않아 십신·12운성·통근 비교와 산책 점수를 보류했습니다."})
    natal_pillars = {key: _enrich(pillar, day_master, is_natal_day=key == "day") for key, pillar in stable.items()}
    today_pillars = {key: _enrich(raw_today["pillars"][key], day_master) for key in ("year", "month", "day")}
    natal = {"time_known": known, "pillars": natal_pillars,
             "day_master": _stem(day_master) if day_master else None,
             "alternatives": {"pillars": alternatives},
             "month_context": _month_context(stable["month"], charts[0]["month_terms"]),
             "root_evidence": _root_evidence(natal_pillars, day_master),
             "relations": _relations(natal_pillars), "birth_window": dict(context.get("birth_window", {})),
             "available_pillar_count": sum(pillar is not None for pillar in natal_pillars.values())}
    today = {"pillars": today_pillars, "relative_to_natal_day_master": day_master,
             "month_context": _month_context(raw_today["pillars"]["month"], raw_today["month_terms"]),
             "date": context["target_local"].date().isoformat(),
             "reference_local": context["target_local"].isoformat(),
             "root_evidence": _root_evidence(today_pillars, day_master),
             "profile_uses": "오늘 일주의 천간·지장간만 산책 점수에 반영; 연주·월주는 참고 정보"}
    roots = natal["root_evidence"]
    month = natal["month_context"]
    today_god = today_pillars["day"]["stem_ten_god"]
    highlights = [
        {"label": "일간", "value": f"{_STEM_KO[day_master]}{natal['day_master']['element_korean']}({day_master}) · {natal['day_master']['yin_yang']}" if day_master else "미확정"},
        {"label": "원국", "value": " · ".join(p["text"] if p else "미확정" for p in natal_pillars.values())},
        {"label": "월령", "value": f"{month['month_branch']}월 · 전통 분류 {month['season']}" if month else "월주 미확정"},
        {"label": "같은 오행의 지장간", "value": ", ".join(f"{r['pillar_label']} {r['branch']} 속 {r['hidden_stem']}" for r in roots) or ("확인된 위치 없음" if day_master else "일간 미확정")},
        {"label": "오늘 일주", "value": f"{today_pillars['day']['text']} · 천간 {today_god['korean']}" if today_god else f"{today_pillars['day']['text']} · 관계 미확정"},
        {"label": "분석 범위", "value": f"확정된 원국 {natal['available_pillar_count']}개 기둥 · 신강·용신 판단 제외"},
    ]
    return {
        "system": "saju", "analysis_version": ANALYSIS_VERSION,
        "provider": {"name": "lunar-python", "version": version("lunar_python"), "source": SOURCE, "license": "MIT"},
        "analysis": {"natal": natal, "today": today,
                     "conventions": {"calendar": "gregorian", "year_month": "same instant in fixed UTC+08:00 solar-term reference",
                                     "day_hour": "birthplace civil time; lunar-python sect=2", "true_solar_time": False,
                                     "day_boundary": "00:00; library late-Zi-hour stem convention retained",
                                     "interpretation": "structural relations; not strength probabilities or yongshin",
                                     "today_relations": "relative to the confirmed natal day stem"}},
        "omissions": omissions, "highlights": highlights,
        "display_sections": _display_sections(natal, today),
        "profile": _make_profile(natal, today, day_master),
    }
