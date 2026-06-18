"""auth: authentication tier. The api service calls /verify on every request to validate the
caller's session. Stateless (sessions live in redis), so it is safe to auto-restart. When auth
is unreachable, api can't authenticate anyone — a single auth outage fans out to api, webapp,
gateway and the synthetic users, which the agent must collapse into one `auth:unreachable`
incident attributed to auth."""
import json
import logging
import os
import random
import sys
import time
import uuid

import redis as redis_lib
from flask import Flask, request, jsonify

SERVICE = os.environ.get("SERVICE_NAME", "auth")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")


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

# ---- chaos state ----------------------------------------------------------
CHAOS = {"latency": False, "errors": False}


def chaos_delay():
    if CHAOS["latency"]:
        time.sleep(random.uniform(2.0, 6.0))


def chaos_error():
    return CHAOS["errors"] and random.random() < 0.7


def get_redis():
    return redis_lib.Redis(host=REDIS_HOST, socket_connect_timeout=1, socket_timeout=1)


@app.route("/verify")
def verify():
    rid = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    start = time.time()
    chaos_delay()
    if chaos_error():
        log.error("session validation failed: token store error",
                  extra={"event": "verify_failed", "request_id": rid, "path": "/verify",
                         "status": 500, "error": "auth_error"})
        return jsonify({"error": "auth_error"}), 500
    # touch the session store; degrade gracefully if redis blips (not the focus fault)
    try:
        get_redis().get("session:active")
    except redis_lib.exceptions.RedisError:
        pass
    latency = round((time.time() - start) * 1000, 1)
    log.info("verify ok", extra={"event": "verify", "request_id": rid, "path": "/verify",
                                 "status": 200, "latency_ms": latency})
    return jsonify({"authenticated": True})


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "service": SERVICE})


@app.route("/chaos/<scenario>/<action>", methods=["POST"])
def chaos(scenario, action):
    if scenario in CHAOS and action in ("on", "off"):
        CHAOS[scenario] = action == "on"
        log.warning(f"chaos scenario '{scenario}' turned {action}",
                    extra={"event": "chaos_toggle"})
        return jsonify({"scenario": scenario, "state": action})
    return jsonify({"error": "unknown scenario"}), 404


if __name__ == "__main__":
    log.info("auth starting up", extra={"event": "startup"})
    app.run(host="0.0.0.0", port=5000, threaded=True)
