"""Small local JSON API. A website can consume the returned WGS84 GeoJSON."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def make_server(engine, host="127.0.0.1", port=8765, allowed_origin=None, *, pilot=None, explore=None):
    if (pilot is not None or explore is not None) and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Pilot collection and explore map must bind to a local loopback address")

    class Handler(BaseHTTPRequestHandler):
        server_version = "LuckyPath/0.1"

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if allowed_origin and self.headers.get("Origin") == allowed_origin:
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(200, {"ok": True})

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if (path in ("/explore", "/explore/", "/explore/config", "/explore/context")
                        or path.startswith("/explore/assets/")) and explore is not None:
                    if not self._pilot_origin_ok():
                        self._send(403, {"error": "Explore map is same-origin and local only"})
                    elif path == "/explore/config":
                        self._send(200, explore.config())
                    elif path == "/explore/context":
                        self._send(200, explore.context())
                    else:
                        name = "index.html" if path in ("/explore", "/explore/") else path[len("/explore/assets/"):]
                        asset = explore.asset(name)
                        if asset is None:
                            self._send(404, {"error": "Map asset not found"})
                        else:
                            data, content_type = asset
                            self.send_response(200)
                            self.send_header("Content-Type", content_type)
                            self.send_header("Content-Length", str(len(data)))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                            self.end_headers()
                            self.wfile.write(data)
                elif path in ("/pilot", "/pilot/manifest") and pilot is not None:
                    if not self._pilot_origin_ok():
                        self._send(403, {"error": "Pilot collection is same-origin and local only"})
                    elif path == "/pilot/manifest":
                        self._send(200, pilot.public_manifest())
                    else:
                        from .pilot import pilot_html
                        data = pilot_html()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                        self.end_headers()
                        self.wfile.write(data)
                elif path in ("/", "/health"):
                    endpoints = ["GET /metadata", "GET /layers/roads", "POST /route"]
                    if pilot is not None:
                        endpoints += ["GET /pilot", "GET /pilot/manifest", "POST /pilot/assignment", "POST /feedback"]
                    if explore is not None:
                        endpoints += ["GET /explore", "GET /explore/config", "GET /explore/context", "POST /explore/profile", "POST /explore/route", "POST /explore/search", "POST /explore/weather", "POST /explore/reference", "POST /explore/external/settings"]
                    self._send(200, {"ok": True, "service": "LuckyPath", "endpoints": endpoints})
                elif path == "/metadata":
                    self._send(200, engine.metadata())
                elif path.startswith("/layers/"):
                    self._send(200, engine.layer(path[len("/layers/"):]))
                else:
                    self._send(404, {"error": "Endpoint not found"})
            except (BrokenPipeError, ConnectionResetError):
                # A superseded browser request was cancelled; no second response.
                return
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except OSError:
                self._send(500, {"error": "Map data could not be read"})

        def do_POST(self):
            path = urlsplit(self.path).path
            is_pilot = path in ("/pilot/assignment", "/feedback") and pilot is not None
            external_methods = {"/explore/search": "search", "/explore/weather": "weather",
                                "/explore/reference": "reference", "/explore/external/settings": "external_settings"}
            is_explore = (path in ("/explore/route", "/explore/profile") or path in external_methods) and explore is not None
            if path != "/route" and not is_pilot and not is_explore:
                self._send(404, {"error": "Endpoint not found"})
                return
            if (is_pilot or is_explore) and not self._pilot_origin_ok():
                self._send(403, {"error": "This feature is same-origin and local only"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "Use Content-Type: application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    self._send(413, {"error": "Request body must be between 1 and 65536 bytes"})
                    return
                payload = json.loads(self.rfile.read(length))
                if is_pilot:
                    result = pilot.assign(payload) if path == "/pilot/assignment" else pilot.submit(payload)
                elif is_explore and path == "/explore/profile":
                    result = explore.profile(payload)
                elif is_explore and path in external_methods:
                    result = getattr(explore, external_methods[path])(payload)
                else:
                    result = explore.recommend(payload) if is_explore else engine.recommend(payload)
                self._send(200, result)
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                # Internal file paths and stack traces are not sent to clients.
                self._send(500, {"error": "Feedback storage failed" if is_pilot else "Profile calculation failed" if path == "/explore/profile" else "External information could not be retrieved" if path in external_methods else "Route calculation failed"})

        def _pilot_origin_ok(self):
            host = self.headers.get("Host", "")
            if urlsplit("http://" + host).hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
            return self.headers.get("Origin", "http://" + host) == "http://" + host and self.headers.get("Sec-Fetch-Site") != "cross-site"

        def log_message(self, format, *args):
            # Pilot responses and request metadata are not written to access logs.
            if urlsplit(self.path).path not in ("/pilot", "/pilot/manifest", "/pilot/assignment", "/feedback", "/explore/profile", "/explore/search", "/explore/weather", "/explore/reference", "/explore/external/settings"):
                super().log_message(format, *args)

    return ThreadingHTTPServer((host, port), Handler)


def serve(engine, host="127.0.0.1", port=8765, allowed_origin=None, *, pilot=None, explore=None):
    server = make_server(engine, host, port, allowed_origin, pilot=pilot, explore=explore)
    print(f"LuckyPath API: http://{host}:{server.server_address[1]}", flush=True)
    if pilot is not None:
        print(f"Local pilot: http://{host}:{server.server_address[1]}/pilot", flush=True)
    if explore is not None:
        print(f"Local map: http://{host}:{server.server_address[1]}/explore", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
