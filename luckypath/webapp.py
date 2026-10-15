"""Public demo adapter. The separate local/pilot server remains loopback-only."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import html
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, request, send_file
from werkzeug.exceptions import HTTPException


CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def public_origin(value):
    """Require one canonical HTTPS origin; never trust forwarded host headers."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("Set LUCKYPATH_PUBLIC_ORIGIN to the deployed HTTPS origin")
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment or parts.port not in (None, 443)
            or parts.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("LUCKYPATH_PUBLIC_ORIGIN must be an HTTPS hostname without a path")
    return "https://" + parts.hostname.lower()


class RequestBudget:
    """Small single-worker demo budget. Stores timestamps only, never IPs/inputs."""
    def __init__(self, limits=None, clock=time.monotonic):
        self.limits = limits or {"profile": 20, "route": 120, "external": 30}
        self.clock = clock
        self.lock = threading.Lock()
        self.events = {key: deque() for key in self.limits}

    def take(self, group):
        with self.lock:
            now = self.clock()
            queue = self.events[group]
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= self.limits[group]:
                return False
            queue.append(now)
            return True


def check_analysis_runtime():
    """Synthetic startup check: prove both bundled providers work without downloads."""
    from .independent_profile import build_independent_profile
    result = build_independent_profile({
        "birth_date": "1990-05-15", "birth_time": "14:30",
        "birth_timezone": "Asia/Seoul", "target_date": "2026-10-13",
    })
    if result.get("profile_status") != "ready" or not result.get("combined"):
        raise RuntimeError("Bundled Saju/astrology runtime did not pass startup validation")


def create_app(*, explore=None, origin=None, data_path=None, source_archive=None,
               budget=None, check_runtime=True):
    origin = public_origin(origin or os.environ.get("LUCKYPATH_PUBLIC_ORIGIN")
                           or os.environ.get("RENDER_EXTERNAL_URL"))
    root = Path(__file__).resolve().parents[1]
    if explore is None:
        from .engine import Engine
        from .explore import ExploreService
        data_path = Path(data_path or os.environ.get("LUCKYPATH_DATA_DIR", root / "data"))
        explore = ExploreService(Engine(data_path), data_path)
    context = explore.context()  # Missing/invalid deployment data fails at boot.
    if check_runtime:
        check_analysis_runtime()
    archive = Path(source_archive or os.environ.get("LUCKYPATH_SOURCE_ARCHIVE", root / "public/luckypath-source.zip"))
    budget = budget or RequestBudget()
    slots = threading.BoundedSemaphore(2)
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=65536, DEBUG=False, TESTING=False,
                      PROPAGATE_EXCEPTIONS=False)
    authority = urlsplit(origin).netloc

    def json_response(value, status=200):
        return Response(json.dumps(value, ensure_ascii=False, allow_nan=False),
                        status=status, content_type="application/json; charset=utf-8")

    @app.before_request
    def boundary():
        # A non-sensitive readiness response also works for provider-internal probes.
        if request.path == "/health" and request.method in ("GET", "HEAD"):
            return None
        if request.headers.get("Host", "").lower() not in (authority, authority + ":443"):
            return json_response({"error": "This hostname is not configured for the demo."}, 400)
        if request.method == "POST":
            if (request.headers.get("Origin") != origin
                    or request.headers.get("Sec-Fetch-Site") == "cross-site"):
                return json_response({"error": "Use the demo from its own website."}, 403)
            if request.mimetype != "application/json":
                return json_response({"error": "Use Content-Type: application/json."}, 415)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return json_response({"error": {
            400: "Invalid JSON request.", 404: "Not found.", 405: "Method not allowed.",
            413: "Request is too large (maximum 64 KB).", 415: "Use JSON for this request.",
        }.get(exc.code, "The request could not be processed.")}, exc.code or 500)

    @app.errorhandler(Exception)
    def error(exc):
        # No exception messages, body, timestamps, birth details or coordinates in logs.
        app.logger.error("Demo request failed (%s)", type(exc).__name__)
        return json_response({"error": "The calculation could not be completed. Please try again."}, 500)

    @app.get("/health")
    def health():
        return json_response({"ok": True, "service": "LuckyPath", "mode": "public-demo"})

    @app.get("/")
    def home():
        return Response(status=302, headers={"Location": "/explore"})

    @app.get("/explore")
    @app.get("/explore/")
    def index():
        return asset("index.html")

    @app.get("/explore/assets/<path:name>")
    def asset(name):
        result = explore.asset(name)
        if result is None:
            return json_response({"error": "Asset not found."}, 404)
        body, content_type = result
        return Response(body, content_type=content_type)

    @app.get("/explore/config")
    def config():
        result = deepcopy(explore.config())
        result["deployment"] = {"mode": "public-demo", "source_url": "/source", "privacy_url": "/privacy"}
        result["external_services"]["ors"]["can_manage_key"] = False
        return json_response(result)

    @app.get("/explore/context")
    def map_context():
        return json_response(context)

    methods = {"profile": ("profile", "profile"), "route": ("recommend", "route"),
               "search": ("search", "external"), "weather": ("weather", "external"),
               "reference": ("reference", "external")}

    @app.post("/explore/<action>")
    def calculate(action):
        if action not in methods:
            return json_response({"error": "Not found."}, 404)
        payload = request.get_json()
        if not isinstance(payload, dict):
            return json_response({"error": "Request must be a JSON object."}, 400)
        method, group = methods[action]
        if not budget.take(group):
            response = json_response({"error": "The demo is busy. Please try again in a minute."}, 429)
            response.headers["Retry-After"] = "60"
            return response
        if not slots.acquire(blocking=False):
            response = json_response({"error": "The demo is calculating another trip. Please try again shortly."}, 503)
            response.headers["Retry-After"] = "3"
            return response
        try:
            return json_response(getattr(explore, method)(payload))
        except (ValueError, TypeError):
            # Production errors never reflect arbitrary submitted field names/values.
            return json_response({"error": "Check your inputs and choose locations inside Williamsburg."}, 400)
        finally:
            slots.release()

    def info_page(title, paragraphs):
        body = "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        body += "<title>" + html.escape(title) + " | LuckyPath</title><body><main><h1>" + html.escape(title) + "</h1>"
        body += "".join("<p>" + p + "</p>" for p in paragraphs)
        body += '<p><a href="/explore">Back to LuckyPath</a></p></main></body></html>'
        return Response(body, content_type="text/html; charset=utf-8")

    @app.get("/privacy")
    def privacy():
        return info_page("Your information", [
            "Birth details are sent over HTTPS to this demo server to calculate the two analyses. The app does not save them to a database or server files, and application request-body/access logging is disabled. The hosting provider can process network metadata under its own policies.",
            "Save details is optional and stores inputs only in this browser. Delete saved details removes that saved profile. There is no account sync. Inputs and calculation results are otherwise held in working memory while used.",
            "Address queries and trip coordinates are sent to openrouteservice when its features are used. Open-Meteo receives an approximate Williamsburg location for weather. Neither receives birth inputs through these integrations.",
            "The interpretation weights and environmental indicators are experimental. Current access, street conditions and arrival times are not verified.",
        ])

    @app.get("/source")
    def source():
        link = ('<a href="/source-download">Download this deployment\u2019s application source, build files and notices</a>.'
                if archive.is_file() else "The source package is not present in this local deployment check.")
        return info_page("Source and licenses", [
            link,
            'Saju uses lunar-python. Astrology uses Kerykeion 6.0.5 and libephemeris 3.2.2, distributed under AGPL-3.0. Their upstream source and license notices are listed in the source package. Third-party components and map data retain their own terms.',
            'Map data: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> and <a href="https://opendata.cityofnewyork.us/">NYC Open Data</a>. Optional external services: openrouteservice / HeiGIT and Open-Meteo.',
        ])

    @app.get("/source-download")
    def source_download():
        if not archive.is_file():
            return json_response({"error": "Source archive not found."}, 404)
        return send_file(archive, mimetype="application/zip", as_attachment=True,
                         download_name="luckypath-source.zip", conditional=True)

    return app


if __name__ == "__main__":
    check_analysis_runtime()
    print("Bundled analysis runtime: OK (synthetic input; no external request).")
