"""Evidence must explain missingness without fabricating quality or route costs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from pyproj import Transformer
from shapely.geometry import LineString, box

from luckypath.data import Dataset, Edge, save_dataset
from luckypath.engine import Engine
from luckypath.evidence import EvidenceIndex, METRICS, validate_evidence_profile
from luckypath.features import prepare_features


def fixture():
    dataset = Dataset()
    dataset.nodes = {str(i): (584000 + 250 * i, 4500000) for i in range(4)}
    dataset.edges = [Edge(f"e{i}", str(i), str(i + 1), [dataset.nodes[str(i)], dataset.nodes[str(i + 1)]],
                          250, {"highway": "residential"}) for i in range(3)]
    dataset.buildings = [box(584050, 4500020, 584100, 4500040)]
    dataset.major_roads = [LineString([(584000, 4499000), (584000, 4501000)])]
    dataset.entrances = [(584000, 4500000)]
    dataset.trees = [(584125, 4500000)]
    # A bank can have a zero social component even though a nearby record exists.
    dataset.places = [{"x": 584125, "y": 4500000, "social_component": 0, "access_component": 0.8,
                       "longitude": -74, "latitude": 40.7, "primary_type": "bank"}]
    dataset.metadata = {
        "source_files": {"trees": {"file": "nyc_street_trees_2015.csv.gz"},
                         "buildings": {"file": "buildings.parquet"},
                         "roads": {"file": "osm/nyc_roads.parquet"},
                         "places": {"file": "places/nyc_osm_places_scored.csv.gz"}},
        "trees": {"included_rows": 1, "status_counts": {"Alive": 1}},
    }
    return dataset, prepare_features(dataset)


def profile():
    return {"schema_version": 1, "profile_id": "test-source-proxy-v1", "metrics": {
        name: {"measurement": f"{name} proxy", "source_date": "2015" if name == "green" else "2026-10-13",
               "sources": ["test-source"], "evidence_status": "partial", "source_available": True,
               "limitations": ["Not human-validated"]} for name in METRICS}}


class EvidenceTests(unittest.TestCase):
    def test_zero_score_with_a_record_is_distinct_from_no_nearby_records(self):
        dataset, features = fixture()
        evidence = EvidenceIndex(dataset, features)
        bank = evidence.edge("e0")["social"]
        empty = evidence.edge("e1")["social"]
        self.assertEqual(bank["raw_measurement"], empty["raw_measurement"])
        self.assertEqual(bank["observation_status"], "observed_zero")
        self.assertEqual(bank["nearby_record_count"], 1)
        self.assertEqual(empty["observation_status"], "no_records_nearby")
        self.assertEqual(empty["nearby_record_count"], 0)
        self.assertFalse(evidence.metadata()["missing_records_mean_absence"])
        self.assertFalse(bank["empirical_quality_verified"])

    def test_missing_tree_source_has_null_observation_even_with_numeric_model_score(self):
        dataset, _ = fixture()
        dataset.trees = []
        dataset.metadata["source_files"].pop("trees")
        dataset.metadata["trees"] = {"provided": False}
        features = prepare_features(dataset)
        record = EvidenceIndex(dataset, features).edge("e0")["green"]
        self.assertEqual(record["model_score"], features["edges"]["e0"]["green"])
        self.assertIsNone(record["observed_score"])
        self.assertIsNone(record["raw_measurement"])
        self.assertIsNone(record["nearby_record_count"])
        self.assertEqual(record["observation_status"], "source_unavailable")

    def test_historical_provenance_is_not_changed_to_current_or_alive_only(self):
        dataset, features = fixture()
        dataset.metadata["trees"]["status_counts"] = {"Dead": 1}
        metadata = EvidenceIndex(dataset, features).metadata()
        self.assertEqual(metadata["metrics"]["green"]["source_date"], "2015")
        self.assertTrue(any("Dead" in text for text in metadata["metrics"]["green"]["limitations"]))
        self.assertIsNone(metadata["metrics"]["social"]["source_date"])
        self.assertIsNone(metadata["confidence_probability"])
        self.assertFalse(metadata["affects_route_cost"])

    def test_alive_only_experiment_preserves_historical_date_and_explicit_policy(self):
        dataset, features = fixture()
        dataset.metadata["evidence"] = profile()
        green = dataset.metadata["evidence"]["metrics"]["green"]
        green["measurement"] = "2015 Alive records within 50 m"
        green["limitations"] = ["Alive in 2015; not current canopy"]
        record = EvidenceIndex(dataset, features).metadata()["metrics"]["green"]
        self.assertEqual(record["source_date"], "2015")
        self.assertEqual(record["measurement"], green["measurement"])
        self.assertFalse(record["empirical_quality_verified"])

    def test_identity_crosscheck_counts_only_nearby_documented_matches(self):
        dataset, features = fixture()
        dataset.metadata["evidence"] = profile()
        dataset.places[0].update(evidence_status="crosschecked", verification_scope="record_identity_only",
                                 verification_source="DOHMH snapshot + OSM")
        evidence = EvidenceIndex(dataset, features)
        row = evidence.edge("e0")["social"]
        self.assertEqual(row["crosschecked_place_records_nearby"], 1)
        self.assertEqual(row["evidence_status"], "partial")
        self.assertEqual(evidence.edge("e1")["social"]["crosschecked_place_records_nearby"], 0)
        self.assertFalse(row["empirical_quality_verified"])
        # A bare label or unsupported quality assertion must not count.
        dataset.places[0].pop("verification_source")
        self.assertEqual(EvidenceIndex(dataset, features).edge("e0")["social"]["crosschecked_place_records_nearby"], 0)

    def test_malformed_or_unsupported_verification_metadata_is_rejected(self):
        mutations = [
            lambda p: p.update(schema_version=2),
            lambda p: p["metrics"].pop("green"),
            lambda p: p["metrics"]["green"].update(evidence_status="90%"),
            lambda p: p["metrics"]["green"].update(evidence_status="crosschecked"),
            lambda p: p["metrics"]["green"].update(source_available="false"),
            lambda p: p["metrics"]["green"].update(sources="source"),
            lambda p: p.update(empirical_quality_verified=True),
            lambda p: p["metrics"]["social"].update(empirical_quality_verified=True),
        ]
        for mutate in mutations:
            value = profile()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_evidence_profile(value)

    def test_partial_and_repeated_route_edges_are_counted_without_length_claims(self):
        dataset, features = fixture()
        evidence = EvidenceIndex(dataset, features)
        result = evidence.route(["e0", "e1", "e0"])
        self.assertEqual(result["unique_source_edges"], 2)
        self.assertEqual(result["metrics"]["social"]["observation_status_counts"],
                         {"observed_zero": 1, "no_records_nearby": 1})
        self.assertIn("not length weighted", result["aggregation_basis"])
        self.assertIsNone(evidence.route([])["metrics"]["green"]["observed_score_range"])
        with self.assertRaisesRegex(ValueError, "unknown source edge"):
            evidence.route(["unknown"])

    def test_evidence_does_not_change_costs_routes_or_existing_scores(self):
        dataset, features = fixture()
        to_lonlat = Transformer.from_crs(32618, 4326, always_xy=True)
        request = {"origin": list(to_lonlat.transform(584025, 4500000)),
                   "destination": list(to_lonlat.transform(584725, 4500000)), "lucky_limit": 1}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "features.json").write_text(json.dumps(features))
            save_dataset(dataset, path / "dataset.json")
            before = Engine(path).recommend(request)
            dataset.metadata["evidence"] = profile()
            for value in dataset.metadata["evidence"]["metrics"].values():
                value["evidence_status"] = "unverified"
            save_dataset(dataset, path / "dataset.json")
            engine = Engine(path)
            after = engine.recommend(request)
            for kind in ("shortest", "personalized"):
                for key in ("length_m", "total_cost", "edge_ids"):
                    self.assertEqual(before[kind][key], after[kind][key])
                self.assertEqual(before[kind]["route"]["geometry"], after[kind]["route"]["geometry"])
                self.assertEqual(after[kind]["evidence"]["unique_source_edges"], 3)
                self.assertEqual(after[kind]["route"]["properties"]["evidence"], after[kind]["evidence"])
            self.assertEqual(engine.metadata()["evidence"]["profile_id"], "test-source-proxy-v1")
            first_road = engine.layer("roads")["features"][0]["properties"]
            for metric in METRICS:
                self.assertEqual(first_road[metric], features["edges"]["e0"][metric])
            self.assertEqual(first_road["evidence"]["social"]["observation_status"], "observed_zero")
            self.assertIn("evidence", after["lucky_stops"][0])
            json.dumps(after, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
