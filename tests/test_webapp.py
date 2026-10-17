"""Exercise the real public adapter without publishing or calling provider APIs."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from luckypath.engine import Engine
from luckypath.explore import ExploreService
from luckypath.webapp import create_app, public_origin, RequestBudget

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("LUCKYPATH_TEST_DATA", ROOT.parent / "luckypath-nyc-data/quality/pilot/williamsburg"))
ORIGIN = "https://demo.example.com"
HEADERS = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
SYNTHETIC = {"birth_date": "1990-05-15", "birth_time": "14:30", "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13"}


class BoundaryTests(unittest.TestCase):
    def test_origin_validation_and_normalization(self):
        self.assertEqual(public_origin("https://Demo.Example.com/"), ORIGIN)
        for bad in (None, "", "http://demo.example.com", "https://localhost", "https://demo.example.com/path",
                    "https://user:secret@demo.example.com", "https://demo.example.com?x=y",
                    "https://demo.example.com#fragment", "https://demo.example.com:444", "https://demo.example.com\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): public_origin(bad)

    def test_budget_expires_and_keeps_only_timestamps(self):
        now = [0.]
        budget = RequestBudget({"route": 2}, clock=lambda: now[0])
        self.assertTrue(budget.take("route")); self.assertTrue(budget.take("route"))
        self.assertFalse(budget.take("route"))
        now[0] = 60.
        self.assertTrue(budget.take("route"))
        self.assertEqual(list(budget.events["route"]), [60.])


@unittest.skipUnless((DATA / "dataset.json").is_file(), "Requires prepared demo data")
class PublicDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"LUCKYPATH_ORS_API_KEY": "", "ORS_API_KEY": "", "OPENROUTESERVICE_API_KEY": ""}):
            cls.explore = ExploreService(Engine(DATA), DATA)
        # Includes an actual offline Saju + astrology cold-start check.
        cls.app = create_app(explore=cls.explore, origin=ORIGIN, check_runtime=True)

    def setUp(self):
        self.client = self.app.test_client()

    def get(self, path, **kwargs):
        return self.client.get(path, base_url=ORIGIN, **kwargs)

    def post(self, path, payload, **kwargs):
        return self.client.post(path, base_url=ORIGIN, json=payload, headers=HEADERS, **kwargs)

    def test_home_health_assets_config_and_context(self):
        self.assertEqual(self.get('/').headers['Location'], '/explore')
        self.assertEqual(self.client.get('/health', base_url='http://internal:10000').status_code, 200)
        page = self.get('/explore')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'ors-settings-panel', page.data)
        self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])
        for name in ('app.js', 'external.js', 'styles.css', 'vendor/leaflet.js'):
            self.assertEqual(self.get('/explore/assets/' + name).status_code, 200)
        config = self.get('/explore/config').json
        self.assertFalse(config['external_services']['ors']['can_manage_key'])
        self.assertFalse(config['external_services']['ors']['configured'])
        self.assertEqual(config['deployment']['mode'], 'public-demo')
        self.assertEqual(self.get('/explore/context').json['type'], 'FeatureCollection')

    def test_host_origin_and_forwarding_spoof_are_rejected(self):
        self.assertEqual(self.client.get('/explore', base_url='https://attacker.example').status_code, 400)
        for headers in ({}, {'Origin': 'https://attacker.example'}, {'Origin': 'null'},
                        {**HEADERS, 'Sec-Fetch-Site': 'cross-site'},
                        {'Origin': 'https://attacker.example', 'X-Forwarded-Host': 'demo.example.com', 'X-Forwarded-Proto': 'https'}):
            response = self.client.post('/explore/route', base_url=ORIGIN, json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_only_embed_document_allows_the_exact_portfolio_parents(self):
        for path in ('/embed', '/embed/'):
            page = self.get(path)
            self.assertEqual(page.status_code, 200)
            policy = page.headers['Content-Security-Policy']
            ancestors = next(part.strip() for part in policy.split(';') if 'frame-ancestors' in part)
            self.assertEqual(ancestors.split()[1:], [
                'https://jayce-portfolio-phi.vercel.app',
                'http://127.0.0.1:3000', 'http://localhost:3000'])
            self.assertNotIn('X-Frame-Options', page.headers)
            self.assertIn('href="/embed"', page.text)
            self.assertIn('href="/source" target="_blank"', page.text)
            self.assertIn('href="/privacy" target="_blank"', page.text)
        for path in ('/explore', '/explore/', '/privacy', '/source', '/explore/config', '/embed/missing'):
            page = self.get(path)
            self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])
            self.assertEqual(page.headers['X-Frame-Options'], 'DENY')
        # The portfolio may frame the UI, but may not call the API as its own origin.
        response = self.client.post('/explore/route', base_url=ORIGIN, json={},
                                    headers={'Origin': 'https://jayce-portfolio-phi.vercel.app',
                                             'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_public_key_changes_pilot_and_unbundled_files_are_unavailable(self):
        with patch.object(self.explore, 'external_settings', side_effect=AssertionError('must not execute')):
            self.assertEqual(self.post('/explore/external/settings', {'ors_api_key': 'synthetic-canary'}).status_code, 404)
        for path in ('/pilot', '/pilot/manifest', '/metadata', '/layers/roads', '/explore/assets/../webapp.py', '/.env'):
            self.assertEqual(self.get(path).status_code, 404)
        for path in ('/feedback', '/route', '/pilot/assignment'):
            self.assertEqual(self.post(path, {}).status_code, 404)

    def test_real_route_and_local_search_without_any_external_key(self):
        result = self.post('/explore/route', self.explore.initial_request)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['personalized'])
        self.assertTrue(result.json['comparison']['shortest'])
        result = self.post('/explore/search', {'query': 'Domino'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['provider'], 'local')

    def test_actual_profile_calculates_both_systems_and_combines(self):
        response = self.post('/explore/profile', SYNTHETIC)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['profile_status'], 'ready')
        self.assertEqual(set(response.json['systems']), {'saju', 'astrology'})
        self.assertEqual(len(response.json['combined']['profile_scores']), 5)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_input_size_type_and_schema_are_enforced(self):
        self.assertEqual(self.client.post('/explore/route', base_url=ORIGIN, data='{}', headers=HEADERS).status_code, 415)
        self.assertEqual(self.post('/explore/route', []).status_code, 400)
        self.assertEqual(self.post('/explore/route', {'padding': 'x' * 65536}).status_code, 413)
        self.assertEqual(self.client.post('/explore/route', base_url=ORIGIN, data='{', headers={**HEADERS, 'Content-Type': 'application/json'}).status_code, 400)
        payload = {**self.explore.initial_request, 'origin': [-74,40.8]}
        self.assertEqual(self.post('/explore/route', payload).status_code, 400)

    def test_internal_failure_does_not_log_or_return_private_values(self):
        canary = 'private-canary-birth-and-key'
        with patch.object(self.explore, 'recommend', side_effect=RuntimeError(canary)):
            with self.assertLogs(self.app.logger, level='ERROR') as logs:
                response = self.post('/explore/route', self.explore.initial_request)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(canary, response.text)
        self.assertNotIn(canary, '\n'.join(logs.output))

    def test_rate_limit_blocks_calculation_and_has_retry_hint(self):
        app = create_app(explore=self.explore, origin=ORIGIN, check_runtime=False,
                         budget=RequestBudget({'profile':1,'route':1,'external':1}))
        client = app.test_client()
        for expected in (200,429):
            response = client.post('/explore/search', base_url=ORIGIN, json={'query':'Domino'}, headers=HEADERS)
            self.assertEqual(response.status_code, expected)
        self.assertEqual(response.headers['Retry-After'], '60')

    def test_source_and_privacy_are_available_without_dynamic_file_access(self):
        self.assertEqual(self.get('/privacy').status_code, 200)
        self.assertEqual(self.get('/source').status_code, 200)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.zip'; archive.write_bytes(b'synthetic archive')
            app=create_app(explore=self.explore, origin=ORIGIN, check_runtime=False, source_archive=archive)
            response=app.test_client().get('/source-download', base_url=ORIGIN)
            self.assertEqual(response.data, b'synthetic archive')
            self.assertIn('attachment', response.headers['Content-Disposition'])
            response.close()


if __name__ == '__main__': unittest.main()
