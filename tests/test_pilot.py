"""Validate opt-in collection, version binding and exclusion of non-field data."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from luckypath.pilot import PilotStore, export_feedback, read_feedback, summarize_feedback, validate_manifest
from luckypath.server import make_server


def fixture_manifest():
    return {"schema_version": 1, "pilot_id": "unit-test-only", "route_pairs": [{
        "pair_id": "test-pair", "neighborhood": "Test neighborhood", "label": "Synthetic fixture, not a field observation",
        "profile_scores": {"open": 6, "green": 9, "still": 4, "social": 7, "access": 5},
        "model_version": "test-model", "calibration_id": "test-calibration", "dataset_sha256": "a" * 64, "features_sha256": "b" * 64,
        "routes": {"shortest": {"length_m": 100, "geometry": {"type": "LineString", "coordinates": [[-74, 40.7], [-73.999, 40.7]]}},
                   "personalized": {"length_m": 120, "geometry": {"type": "LineString", "coordinates": [[-74, 40.7], [-73.9995, 40.7002], [-73.999, 40.7]]}}}}]}


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manifest_path = self.folder / "manifest.json"
        self.manifest_path.write_text(json.dumps(fixture_manifest()))
        self.db = self.folder / "feedback.sqlite"
        self.store = PilotStore(self.manifest_path, self.db)

    def assignment_request(self, **changes):
        return {"assignment_id": str(uuid4()), "participant_code": "walk_07", "pair_id": "test-pair",
                "manifest_sha256": self.store.manifest_sha256, "consent": True, "initial_intention": "Quiet walk",
                "profile_scores": fixture_manifest()["route_pairs"][0]["profile_scores"], **changes}

    def feedback_request(self, assignment=None, **changes):
        if assignment is None:
            assignment = self.store.assign(self.assignment_request())
        return {"submission_id": str(uuid4()), "assignment_id": assignment["assignment_id"],
                "pair_id": assignment["pair_id"], "route_variant": "A", "manifest_sha256": assignment["manifest_sha256"],
                "route_version": assignment["route_version"], "consent": True, "profile_scores": assignment["profile_scores"],
                "walk_completed": True, "is_test": False, "walk_date": "2026-01-01", "time_of_day": "afternoon",
                "weather": "clear", "ratings": {"quiet": 3, "green": 4, "walkability": 2, "goal_fit": 4, "detour_worth": 3},
                "issue": "none", "issue_note": "", **changes}

    def test_empty_database_reports_no_field_feedback(self):
        summary = summarize_feedback(self.db)
        self.assertEqual(summary["status"], "no field feedback collected")
        self.assertEqual(summary["groups"], [])
        self.assertEqual(summary["eligible_submissions"], 0)

    def test_public_manifest_does_not_reveal_condition_labels_or_versions_of_other_data(self):
        result = self.store.public_manifest()
        self.assertNotIn("shortest", json.dumps(result))
        self.assertNotIn("personalized", json.dumps(result))
        self.assertEqual(len(result["manifest_sha256"]), 64)

    def test_assignment_is_idempotent_and_condition_mapping_is_deterministic(self):
        request = self.assignment_request()
        a = self.store.assign(request)
        self.assertEqual(a, self.store.assign(request))
        b = self.store.assign({**request, "assignment_id": str(uuid4())})
        self.assertEqual(a["routes"], b["routes"])
        self.assertEqual(a["presented_order"], b["presented_order"])
        self.assertNotIn("condition_by_variant", a)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.store.assign({**request, "initial_intention": "Changed"})

    def test_label_and_order_assignment_varies_between_pseudonyms(self):
        assignments = [self.store.assign(self.assignment_request(participant_code=f"walk_{i}")) for i in range(30)]
        self.assertEqual({a["routes"]["A"]["length_m"] for a in assignments}, {100, 120})
        self.assertEqual({tuple(a["presented_order"]) for a in assignments}, {("A", "B"), ("B", "A")})

    def test_consent_profile_and_manifest_mismatch_rejected(self):
        for changes in ({"consent": False}, {"manifest_sha256": "0" * 64}, {"participant_code": "real@example.com"}, {"pair_id": "unknown"},
                        {"profile_scores": {"open": 1, "green": 1, "still": 1, "social": 1, "access": 1}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.assign(self.assignment_request(**changes))

    def test_valid_feedback_stores_versions_and_is_idempotent(self):
        request = self.feedback_request()
        self.assertFalse(self.store.submit(request)["duplicate"])
        self.assertTrue(self.store.submit(request)["duplicate"])
        rows = read_feedback(self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["analysis_eligible"])
        for key in ("route_version", "manifest_sha256", "profile_scores"):
            self.assertEqual(row[key], request[key])
        self.assertEqual(row["calibration_id"], "test-calibration")
        self.assertEqual(row["dataset_sha256"], "a" * 64)
        self.assertIn(row["condition"], ("shortest", "personalized"))
        with self.assertRaisesRegex(ValueError, "different feedback"):
            self.store.submit({**request, "weather": "rain"})
        with self.assertRaisesRegex(ValueError, "already has feedback"):
            self.store.submit({**request, "submission_id": str(uuid4())})

    def test_feedback_bound_to_known_assignment_and_route_version(self):
        request = self.feedback_request()
        for changes in ({"assignment_id": str(uuid4())}, {"pair_id": "unknown"}, {"route_version": "0" * 64},
                        {"manifest_sha256": "0" * 64}, {"route_variant": "shortest"}, {"consent": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.submit({**request, **changes})
        self.assertEqual(read_feedback(self.db), [])

    def test_strict_ratings_metadata_and_unknown_personal_fields(self):
        request = self.feedback_request()
        mutations = [{"ratings": {**request["ratings"], "quiet": v}} for v in (0, 6, True, 3.5, float("nan"))]
        mutations += [{"ratings": {}}, {"is_test": "false"}, {"walk_completed": 1}, {"weather": "not-a-choice"},
                      {"walk_date": "2099-01-01"}, {"walk_date": "2026-02-30"}, {"issue_note": "x" * 501}, {"email": "not-accepted@example.com"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.store.submit({**request, **mutation})

    def test_test_and_unwalked_responses_do_not_become_quality_evidence(self):
        self.store.submit(self.feedback_request(is_test=True))
        self.store.submit(self.feedback_request(walk_completed=False, ratings={}))
        summary = summarize_feedback(self.db)
        self.assertEqual(summary["status"], "no field feedback collected")
        self.assertEqual(summary["excluded_test_or_not_walked"], 2)
        self.assertEqual(summary["groups"], [])
        out = self.folder / "eligible.jsonl"
        self.assertEqual(export_feedback(self.db, out, eligible_only=True)["rows"], 0)
        self.assertEqual(out.read_text(), "")

    def test_within_pair_comparison_requires_both_completed_variants(self):
        assignment = self.store.assign(self.assignment_request())
        self.store.submit(self.feedback_request(assignment, route_variant="A"))
        self.assertEqual(summarize_feedback(self.db)["complete_pair_comparisons"], [])
        self.store.submit(self.feedback_request(assignment, route_variant="B"))
        summary = summarize_feedback(self.db)
        self.assertEqual(len(summary["complete_pair_comparisons"]), 1)
        self.assertEqual(set(summary["complete_pair_comparisons"][0]["personalized_minus_shortest"].values()), {0})

    def test_export_keeps_provenance_and_csv_text_is_inert(self):
        self.store.submit(self.feedback_request(issue_note="=SUM(1,2)"))
        out = self.folder / "feedback.csv"
        export_feedback(self.db, out, "csv")
        content = out.read_text()
        self.assertIn("dataset_sha256", content)
        self.assertIn("'=SUM(1,2)", content)
        out = self.folder / "feedback.jsonl"
        export_feedback(self.db, out)
        self.assertEqual(json.loads(out.read_text())["issue_note"], "=SUM(1,2)")

    def test_manifest_invalid_routes_and_source_hashes_rejected(self):
        manifest = fixture_manifest()
        for key, value in (("features_sha256", "bad"), ("profile_scores", {"green": 5})):
            bad = copy.deepcopy(manifest)
            bad["route_pairs"][0][key] = value
            with self.assertRaises(ValueError):
                validate_manifest(bad)
        bad = copy.deepcopy(manifest)
        bad["route_pairs"][0]["routes"]["personalized"]["geometry"]["coordinates"][-1][0] = -70
        with self.assertRaisesRegex(ValueError, "endpoints"):
            validate_manifest(bad)

    def test_server_endpoints_are_opt_in_same_origin_and_local(self):
        class StubEngine:
            def recommend(self, payload): return {"legacy_route": payload}
            def metadata(self): return {"legacy": True}
        server = make_server(StubEngine(), port=0, pilot=self.store)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def request_http(method, path, payload=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            try:
                data = json.dumps(payload) if payload is not None else None
                conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
                res = conn.getresponse()
                return res.status, dict(res.getheaders()), res.read()
            finally:
                conn.close()
        try:
            status, headers, html = request_http("GET", "/pilot")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn(b"geolocation", html)
            self.assertEqual(request_http("GET", "/pilot/manifest")[0], 200)
            self.assertEqual(request_http("POST", "/pilot/assignment", self.assignment_request(), {"Origin": "https://elsewhere.example"})[0], 403)
            self.assertEqual(request_http("GET", "/pilot", headers={"Host": "rebind.example"})[0], 403)
            status, _, assigned = request_http("POST", "/pilot/assignment", self.assignment_request())
            self.assertEqual(status, 200)
            status, _, _ = request_http("POST", "/feedback", self.feedback_request(json.loads(assigned)))
            self.assertEqual(status, 200)
            status, _, result = request_http("POST", "/route", {"original_api": True})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(result), {"legacy_route": {"original_api": True}})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(StubEngine(), host="0.0.0.0", port=0, pilot=self.store)
        server = make_server(StubEngine(), port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            self.assertEqual(request_http("GET", "/pilot")[0], 404)
            self.assertEqual(request_http("POST", "/feedback", {})[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
