"""Single-worker small demo: bounded shared caches/budgets, no request logging."""
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = None
errorlog = "-"
loglevel = "warning"
capture_output = False
preload_app = False
forwarded_allow_ips = ""  # Origin/host checks use a configured URL, not proxy input.
limit_request_line = 4094
limit_request_fields = 60
limit_request_field_size = 8190
max_requests = 1000
max_requests_jitter = 100

