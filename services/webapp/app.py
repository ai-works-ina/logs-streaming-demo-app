"""webapp: front-tier service. Receives traffic from the gateway, calls the api service."""
import json
import logging
import os
import sys
import time
import uuid

import requests
from flask import Flask, request, jsonify

SERVICE = os.environ.get("SERVICE_NAME", "webapp")
API_URL = os.environ.get("API_URL", "http://api:5000")
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "3"))


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "service": SERVICE,
            "level": record.levelname,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        for key in ("request_id", "path", "status", "latency_ms", "error"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.addHandler(handler)
logging.getLogger("werkzeug").disabled = True

app = Flask(__name__)

# ---- prometheus RED metrics (added for the SRE agent's MetricSource path) --------
from prometheus_client import Counter as _Counter, Histogram as _Histogram, make_wsgi_app as _mwa
from werkzeug.middleware.dispatcher import DispatcherMiddleware as _Dispatcher
from flask import g as _g

_HTTP_REQS = _Counter("http_requests_total", "HTTP requests", ["service", "path", "status"])
_HTTP_LAT = _Histogram("http_request_duration_seconds", "HTTP request latency (seconds)",
                       ["service", "path"])


@app.before_request
def _metrics_start():
    _g._t0 = time.time()


@app.after_request
def _metrics_record(resp):
    path = request.url_rule.rule if request.url_rule else request.path
    _HTTP_REQS.labels(SERVICE, path, resp.status_code).inc()
    _HTTP_LAT.labels(SERVICE, path).observe(time.time() - getattr(_g, "_t0", time.time()))
    return resp


app.wsgi_app = _Dispatcher(app.wsgi_app, {"/metrics": _mwa()})


def call_api(path, req_id, method="GET", payload=None):
    headers = {"X-Request-ID": req_id}
    if method == "POST":
        return requests.post(API_URL + path, json=payload, headers=headers, timeout=TIMEOUT)
    return requests.get(API_URL + path, headers=headers, timeout=TIMEOUT)


def handle(path, api_path, method="GET", payload=None):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    start = time.time()
    try:
        resp = call_api(api_path, req_id, method, payload)
        latency = round((time.time() - start) * 1000, 1)
        level = logging.INFO if resp.status_code < 500 else logging.ERROR
        log.log(level, f"{method} {path} -> api {resp.status_code}",
                extra={"event": "request", "request_id": req_id, "path": path,
                       "status": resp.status_code, "latency_ms": latency})
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.Timeout:
        latency = round((time.time() - start) * 1000, 1)
        log.error(f"{method} {path} timed out calling api after {TIMEOUT}s",
                  extra={"event": "upstream_timeout", "request_id": req_id, "path": path,
                         "status": 504, "latency_ms": latency, "error": "api_timeout"})
        return jsonify({"error": "upstream timeout"}), 504
    except requests.exceptions.ConnectionError:
        latency = round((time.time() - start) * 1000, 1)
        log.error(f"{method} {path} failed: cannot connect to api service",
                  extra={"event": "upstream_down", "request_id": req_id, "path": path,
                         "status": 502, "latency_ms": latency, "error": "api_unreachable"})
        return jsonify({"error": "upstream unavailable"}), 502


@app.route("/")
def index():
    return jsonify({"service": SERVICE, "status": "ok"})


@app.route("/products")
def products():
    return handle("/products", "/products")


@app.route("/checkout", methods=["POST"])
def checkout():
    return handle("/checkout", "/orders", "POST",
                  {"item": request.json.get("item", "widget") if request.is_json else "widget"})


@app.route("/profile")
def profile():
    return handle("/profile", "/users/lookup")


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "service": SERVICE})


if __name__ == "__main__":
    log.info("webapp starting up", extra={"event": "startup"})
    app.run(host="0.0.0.0", port=5000, threaded=True)
