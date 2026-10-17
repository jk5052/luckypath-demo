Original Route Maker interpretation rules

Origin: route_maker/config and route_maker/engine/combine_reading.py.
Copyright (c) 2026 jk5052. Original material is MIT licensed; see LICENSE.txt.
source_manifest.json records SHA-256 digests for the three unchanged CSV files.
The live demo is distributed under its repository's AGPL-3.0 terms; the
original MIT attribution is retained here.

Preserved pipeline
1. Saju and astrology fire their own CSV rules separately.
2. Each source accumulates 3 events and 6 qualities; each value is saturated
   with 1-exp(-score) and rounded to 3 decimals.
3. Source event/quality values combine 50:50, rounded to 3 decimals.
4. Combined qualities pass through the complete 6-by-8 street matrix. All
   eight weights are normalized together and rounded to 4 decimals.
5. The real-street demo then selects green, quiet, open_sky and vibrant,
   renormalizing these four without additional rounding for route cost.

The four connected GIS attributes are calibrated proxies, not direct live
measurements of noise, sky view or footfall. Water, sunlight, rest and culture
are retained in the original eight-axis explanation but omitted from live
route cost because no calibrated graph attribute is connected. They are not
set to zero. Convenience remains a neutral 5/10 for nearby place suggestions.
Display sliders scale the four supported weights relative to their largest
weight (1+9*w/max(w), half-up rounding); automatic routing uses the unrounded
four-axis projection, not these display values. Moving a slider switches to
the manual preference policy.

Factual-provider changes
- Existing precise lunar-python and Kerykeion/libephemeris providers are kept.
  No approximate fallback is used. JPL-derived positions do not validate the
  interpretation, numeric weights or any claimed route benefit.
- Birth astrology uses supplied civil time and timezone, not assumed UTC noon.
  The daily reference is New York noon. Unknown birth time omits personal
  transit aspects; current Sun/Moon element rules remain available.
- Saju solar-term year/month boundaries use the same instant in UTC+08:00;
  day/hour pillars use birthplace civil time. Uncertain pillars are omitted.
- Saju branch pairs are order-independent. This corrects the original sorted
  tuple mismatch that could miss valid combinations or clashes.
- Original nine planets, aspect factors and maximum orbs are retained.
  Positions are rounded to two decimals for the original aspect adapter;
  strengths are rounded to three decimals, minimum strength is 0.35, at most
  six strongest aspects fire, and natal-planet contributions have factor 0.5.
  This calculation uses the new provider positions, so values need not equal
  the original astronomy-engine/UTC-noon charts.

Interpretation limits
The original CSV explanations, basis types, references and verification
statuses are retained. A verified reference only supports the stated symbolic
background. Event/quality assignments, all numeric initial weights, the
50:50 mix and the street matrix are experimental design choices. They are not
scientifically validated predictions, probabilities, or measurements of a
person's needs. No real birth record or original synthetic street graph was
copied into this package. The original source folder is unchanged.
