"""Independent fixed-scale invariance, cache integrity, and route audit tests."""
import contextlib
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from shapely.geometry import LineString, box

from luckypath.calibration import (
    METHOD, RADII, RAW_FIELDS, SOURCE_KEYS, calibration_digest, recalibrate_features,
    validate_calibration, validate_feature_calibration,
)
from luckypath.cli import main
from luckypath.data import Dataset, Edge, TO_LONLAT, save_dataset
from luckypath.engine import Engine
from luckypath.features import prepare_features


def profile():
    return {
        "schema_version": 1, "id": "synthetic-reference-v1", "method": METHOD, "crs": "EPSG:32618",
        "parameters": dict(RADII),
        "metrics": {name: {"raw_field": raw, "lower": 0, "upper": cap}
                    for (name, raw), cap in zip(RAW_FIELDS.items(), (20, 40, 10, 200))},
        "provenance": {"source_sha256": {name: str(i) * 64 for i, name in enumerate(sorted(SOURCE_KEYS), 1)},
                       "note": "Synthetic analytic test reference; not fitted to NYC"},
    }


def raw_edge(green=5, social=10, access=2.5, still=50):
    return {"trees_within_radius": green, "social_raw": social, "access_raw": access,
            "major_road_distance_m": still, "open": 0.4, "linearity": 0.2,
            "waterfront_distance_m": None, "midpoint_xy": [0, 0]}


def cache(edges):
    return {"schema_version": 1, "parameters": dict(RADII), "edges": edges,
            "dataset_sha256": "a" * 64}


def dataset(reference=None):
    reference = reference or profile()
    a, b, c = (585000.0, 4510000.0), (585100.0, 4510000.0), (585200.0, 4510000.0)
    return Dataset(
        nodes={"a": a, "b": b, "c": c},
        edges=[Edge("ab", "a", "b", [a, b], 100, {"highway": "footway"}),
               Edge("bc", "b", "c", [b, c], 100, {"highway": "footway"})],
        buildings=[box(a[0] + 20, a[1] + 20, a[0] + 30, a[1] + 30)],
        trees=[(a[0] + 50, a[1])],
        major_roads=[LineString([(a[0] - 50, a[1] - 100), (a[0] - 50, a[1] + 100)])],
        entrances=[a], metadata={"calibration_source_sha256": copy.deepcopy(reference["provenance"]["source_sha256"])},
    )


def write_fixture(folder, *, calibrated=True):
    reference, data = profile(), dataset()
    save_dataset(data, folder / "dataset.json")
    features = prepare_features(data, calibration=reference if calibrated else None)
    features["dataset_sha256"] = hashlib.sha256((folder / "dataset.json").read_bytes()).hexdigest()
    (folder / "features.json").write_text(json.dumps(features))
    return reference, data, features


class CalibrationTests(unittest.TestCase):
    def test_same_raw_edge_scores_do_not_depend_on_other_window_edges(self):
        alone = recalibrate_features(cache({"shared": raw_edge()}), profile())
        crowded = recalibrate_features(cache({"shared": raw_edge(), "outlier": raw_edge(200, 400, 100, 2000)}), profile())
        for metric in RAW_FIELDS:
            self.assertEqual(alone["edges"]["shared"][metric], 0.25)
            self.assertEqual(alone["edges"]["shared"][metric], crowded["edges"]["shared"][metric])

    def test_zero_is_zero_and_above_reference_cap_is_clipped(self):
        result = recalibrate_features(cache({"zero": raw_edge(0, 0, 0, 0),
                                             "high": raw_edge(21, 41, 11, 201)}), profile())
        for metric in RAW_FIELDS:
            self.assertEqual(result["edges"]["zero"][metric], 0)
            self.assertEqual(result["edges"]["high"][metric], 1)

    def test_raw_values_openness_and_dataset_digest_are_preserved_without_mutation(self):
        original = cache({"a": raw_edge()})
        before = copy.deepcopy(original)
        reference = profile()
        result = recalibrate_features(original, reference)
        self.assertEqual(original, before)
        self.assertEqual(result["dataset_sha256"], original["dataset_sha256"])
        self.assertEqual(result["parameters"]["normalization_scope"], "fixed NYC calibration: synthetic-reference-v1")
        self.assertTrue(result["normalization"]["comparable_across_windows"])
        for key, value in original["edges"]["a"].items():
            self.assertEqual(result["edges"]["a"][key], value)
        reference["id"] = "changed-outside"
        self.assertEqual(result["calibration"]["id"], "synthetic-reference-v1")

    def test_invalid_reference_contracts_are_rejected(self):
        invalid = []
        for upper in (0, -1, math.nan, math.inf, True, "20"):
            item = profile()
            item["metrics"]["green"]["upper"] = upper
            invalid.append(item)
        for name, value in (("schema_version", True), ("schema_version", 2),
                            ("method", "minmax"), ("crs", "EPSG:4326"), ("id", "")):
            item = profile()
            item[name] = value
            invalid.append(item)
        item = profile()
        item["metrics"]["green"]["raw_field"] = "canopy"
        invalid.append(item)
        item = profile()
        item["metrics"]["still"]["lower"] = 10
        invalid.append(item)
        item = profile()
        del item["metrics"]["access"]
        invalid.append(item)
        item = profile()
        item["parameters"]["tree_radius_m"] = 100
        invalid.append(item)
        for index, item in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_calibration(item)

    def test_missing_or_incompatible_feature_radii_are_rejected(self):
        malformed = cache({"a": raw_edge()})
        malformed["parameters"] = None
        with self.assertRaises(ValueError):
            recalibrate_features(malformed, profile())
        for value in (None, 75):
            features = cache({"a": raw_edge()})
            if value is None:
                del features["parameters"]["tree_radius_m"]
            else:
                features["parameters"]["tree_radius_m"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                recalibrate_features(features, profile())

    def test_missing_negative_or_nonfinite_raw_measurements_are_rejected(self):
        for value in (None, -1, math.nan, math.inf, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recalibrate_features(cache({"a": raw_edge(green=value)}), profile())

    def test_digest_is_independent_of_json_key_order_and_detects_provenance_changes(self):
        first = profile()
        reordered = dict(reversed(list(first.items())))
        self.assertEqual(calibration_digest(first), calibration_digest(reordered))
        reordered["provenance"] = {"note": "different source snapshot"}
        self.assertNotEqual(calibration_digest(first), calibration_digest(reordered))

    def test_prepare_with_fixed_reference_matches_recalibrating_the_same_raw_cache(self):
        data = dataset()
        legacy = prepare_features(data)
        direct = prepare_features(data, calibration=profile())
        self.assertEqual(direct, recalibrate_features(legacy, profile()))
        # Counts 1 and 0 are measured independently of both normalization modes.
        self.assertEqual(direct["edges"]["ab"]["green"], 0.05)
        self.assertEqual(direct["edges"]["bc"]["green"], 0)
        self.assertEqual(legacy["edges"]["ab"]["green"], 1)
        data.metadata = {}
        with self.assertRaisesRegex(ValueError, "Dataset sources"):
            prepare_features(data, calibration=profile())

    def test_cache_audit_rejects_changed_caps_metadata_and_scores(self):
        good = recalibrate_features(cache({"a": raw_edge()}), profile())
        self.assertEqual(validate_feature_calibration(good)["id"], "synthetic-reference-v1")
        bad = copy.deepcopy(good)
        bad["calibration"]["metrics"]["green"]["upper"] = 40
        with self.assertRaisesRegex(ValueError, "metadata"):
            validate_feature_calibration(bad)
        bad = copy.deepcopy(good)
        bad["edges"]["a"]["green"] = 0.75
        with self.assertRaisesRegex(ValueError, "score"):
            validate_feature_calibration(bad)
        bad = copy.deepcopy(good)
        del bad["calibration"]
        with self.assertRaisesRegex(ValueError, "no calibration"):
            validate_feature_calibration(bad)

    def test_engine_and_geojson_expose_reference_and_reject_mismatched_source_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            reference, data, _ = write_fixture(folder)
            engine = Engine(folder)
            result = engine.recommend({"origin": list(TO_LONLAT.transform(*data.nodes["a"])),
                                       "destination": list(TO_LONLAT.transform(*data.nodes["c"])), "lucky_limit": 0})
            self.assertAlmostEqual(result["shortest"]["length_m"], 200, places=5)
            self.assertEqual(result["normalization"], engine.metadata()["normalization"])
            self.assertEqual(result["normalization"]["sha256"], calibration_digest(reference))
            for feature in result["geojson"]["features"]:
                self.assertEqual(feature["properties"]["normalization_id"], reference["id"])
            changed = json.loads((folder / "dataset.json").read_text())
            changed["metadata"]["calibration_source_sha256"]["trees"] = "f" * 64
            (folder / "dataset.json").write_text(json.dumps(changed))
            features = json.loads((folder / "features.json").read_text())
            features["dataset_sha256"] = hashlib.sha256((folder / "dataset.json").read_bytes()).hexdigest()
            (folder / "features.json").write_text(json.dumps(features))
            with self.assertRaisesRegex(ValueError, "Dataset sources"):
                Engine(folder)

    def test_cli_recalibrates_only_features_and_rejects_unbound_legacy_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            reference, data, original = write_fixture(folder, calibrated=False)
            before_dataset = (folder / "dataset.json").read_bytes()
            (folder / "reference.json").write_text(json.dumps(reference))
            with contextlib.redirect_stdout(io.StringIO()):
                main(["recalibrate", "--data", str(folder), "--calibration", str(folder / "reference.json")])
            self.assertEqual((folder / "dataset.json").read_bytes(), before_dataset)
            self.assertEqual(Engine(folder).features["edges"]["ab"]["trees_within_radius"],
                             original["edges"]["ab"]["trees_within_radius"])
            data.metadata = {}
            save_dataset(data, folder / "dataset.json")
            original["dataset_sha256"] = hashlib.sha256((folder / "dataset.json").read_bytes()).hexdigest()
            (folder / "features.json").write_text(json.dumps(original))
            before_features = (folder / "features.json").read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["recalibrate", "--data", str(folder), "--calibration", str(folder / "reference.json")])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual((folder / "features.json").read_bytes(), before_features)


if __name__ == "__main__":
    unittest.main()
