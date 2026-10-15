"""Independent analysis -> independent experience weights -> one route profile.

The two domain modules never consume each other's facts or interpretations.
Fusion happens only after each has produced its own explained five scores.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math

from .scoring import WEIGHT_KEYS


PROFILE_VERSION = "independent-analysis-v2"
FUSION_POLICY = "equal-experience-weights-v1"


def combine_profiles(saju, astrology):
    """Give each independent analysis equal weight; round only the final mean.

    Equal weight is an explicit experimental product setting, not a measured
    accuracy or a synthesis of either tradition. Never silently drop a system.
    """
    profiles = {"saju": saju, "astrology": astrology}
    scores, details = {}, {}
    for key in WEIGHT_KEYS:
        values = {}
        for system, profile in profiles.items():
            if not isinstance(profile, dict) or profile.get("status") != "ready":
                raise ValueError("두 분석의 가중치가 모두 준비되어야 적용할 수 있어요.")
            value = profile.get("score_details", {}).get(key, {}).get("unrounded")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 1 <= value <= 10):
                raise ValueError("분석 가중치가 유효하지 않아 적용하지 않았어요.")
            values[system] = Decimal(str(value))
        mean = (values["saju"] + values["astrology"]) / 2
        rounded = int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        scores[key] = rounded
        details[key] = {"saju": float(values["saju"]),
                        "astrology": float(values["astrology"]),
                        "unrounded": float(mean), "rounded": rounded,
                        "difference": float(abs(values["saju"] - values["astrology"]))}
    return {
        "status": "ready", "policy_id": FUSION_POLICY,
        "profile_scores": scores, "score_details": details,
        "policy": {
            "id": FUSION_POLICY, "system_weights": {"saju": .5, "astrology": .5},
            "stage": "after_independent_experience_weights",
            "rounding": "mean of unrounded scores, then nearest integer with half up",
            "externally_validated": False,
            "description": "각 분석이 독립적으로 만든 경험 중요도를 동일 비중으로 평균해 한 경로에 적용해요.",
            "disagreement": "두 분석의 차이는 표에 남기며 일치도·확률로 바꾸지 않아요.",
        },
    }


def build_independent_profile(payload):
    from .analysis_input import prepare_input
    context = prepare_input(payload)
    results = {}
    # Separate lazy imports keep dependency failures isolated and never activate
    # the old mixed-element adapter as a fallback.
    for system in ("saju", "astrology"):
        try:
            if system == "saju":
                from .saju_analysis import analyze_saju
                result = analyze_saju(context)
            else:
                from .astrology_analysis import analyze_astrology
                result = analyze_astrology(context)
            if not isinstance(result, dict) or not isinstance(result.get("profile"), dict):
                raise ValueError("Invalid analysis contract")
            if result["profile"].get("status") == "ready":
                # Validate before fusion so malformed provider results are
                # isolated just like provider failures, never a partial blend.
                combine_profiles(result["profile"], result["profile"])
            results[system] = result
        except Exception:
            label = "사주" if system == "saju" else "점성술"
            results[system] = {
                "system": system, "status": "error", "highlights": [],
                "omissions": [{"field": system, "reason": f"{label} 분석기를 실행하지 못했어요. 설치와 계산 데이터 상태를 확인해 주세요."}],
                "profile": {"status": "unavailable", "profile_scores": None},
            }
    combined = None
    if all(result.get("profile", {}).get("status") == "ready" for result in results.values()):
        combined = combine_profiles(results["saju"]["profile"], results["astrology"]["profile"])
    return {
        "schema_version": 2, "profile_version": PROFILE_VERSION,
        "target": {"date": context["target_local"].date().isoformat(),
                   "timezone": "America/New_York", "reference": "뉴욕 현지 시각 정오"},
        "systems": results, "combined": combined,
        "profile_status": "ready" if combined else "unavailable",
        "notes": ["달력·천체 계산과 경험 가중치 변환은 별도예요.",
                  "두 분석의 경험 가중치와 최종 50:50 비중은 검토 가능한 실험 규칙이에요.",
                  "출생 입력·분석 차트는 경로 요청이나 다운로드에 포함하지 않아요."],
    }
