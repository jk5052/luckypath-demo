"""Independent factual analyses, followed by the original route_maker rules.

The sources are interpreted separately into events and qualities. Their
qualities combine before the street matrix. Display sliders are an adapter;
the automatic router receives the precise supported street-weight projection.
"""
from __future__ import annotations

from copy import deepcopy

from .route_maker_reading import (
    FUSION_POLICY, VERSION, combine_readings, compatibility_profile,
    coverage, interpret_system, provenance,
)


PROFILE_VERSION = VERSION


def build_independent_profile(payload):
    from .analysis_input import prepare_input
    context = prepare_input(payload)
    results, readings = {}, {}
    # The providers only receive shared validated input, never each other's
    # chart or interpretation. No approximate or single-source fallback.
    for system in ("saju", "astrology"):
        try:
            if system == "saju":
                from .saju_analysis import analyze_saju
                result = analyze_saju(context)
            else:
                from .astrology_analysis import analyze_astrology
                result = analyze_astrology(context)
            reading = interpret_system(system, result)
            result = deepcopy(result)
            result["profile"] = compatibility_profile(reading)
            # This former field described the superseded five-axis policy.
            if system == "saju":
                result["analysis"]["today"]["profile_uses"] = (
                    "Original CSV rules: daily stem relation, active elements, "
                    "daily-to-natal branch pairs and confirmed visible element counts."
                )
            results[system], readings[system] = result, reading
        except Exception:
            label = "Saju" if system == "saju" else "Astrology"
            reason = f"The {label} analysis could not be completed. Check the calculation provider and its local data."
            results[system] = {
                "system": system, "status": "error", "highlights": [],
                "omissions": [{"field": system, "reason": reason}],
                "profile": {"status": "unavailable", "profile_scores": None},
            }
            readings[system] = {"status": "unavailable", "event": None, "quality": None,
                                "evidence": [], "source_street_weights": None, "routing_weights": None,
                                "signals": {}, "omissions": [{"field": system, "reason": reason}]}
    reading = combine_readings(readings["saju"], readings["astrology"])
    combined = compatibility_profile(reading, combined=True) if reading else None
    if reading is None:
        reading = {"version": VERSION, "status": "unavailable", **readings,
                   "combined": None, "routing_weights": None, "source_street_weights": None,
                   "coverage": coverage(), "provenance": provenance()}
    reading["sources"] = {system: {"analysis_version": result.get("analysis_version"),
                                   "provider": result.get("provider")}
                          for system, result in results.items()}
    return {
        "schema_version": 2, "profile_version": PROFILE_VERSION,
        "target": {"date": context["target_local"].date().isoformat(),
                   "timezone": "America/New_York", "reference": "New York local noon"},
        "systems": results, "combined": combined, "route_maker_reading": reading,
        "profile_status": "ready" if combined else "unavailable",
        "notes": [
            "Calendar and astronomical positions are separate from experimental interpretation rules.",
            "Original Saju and astrology rules produce separate events and qualities, combined 50:50 before the eight-axis street matrix.",
            "Four connected GIS proxies affect the live route; water, sunlight, rest and culture remain visible but are omitted from route cost.",
            "Birth inputs and analysis charts are not included in route requests or downloads.",
        ],
    }
