"""api: core service. Talks to Postgres and Redis. Hosts chaos toggles for incident injection."""
import json
import logging
import os
import random
import sys
import threading
import time
import uuid

import psycopg2
import redis as redis_lib
import requests
from flask import Flask, request, jsonify

SERVICE = os.environ.get("SERVICE_NAME", "api")
AUTH_URL = os.environ.get("AUTH_URL", "http://auth:5000")
DB_CONF = dict(
    host=os.environ.get("DB_HOST", "postgres"),
    user=os.environ.get("DB_USER", "demo"),
    password=os.environ.get("DB_PASSWORD", "demo"),
    dbname=os.environ.get("DB_NAME", "demo"),
    connect_timeout=2,
)
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
        for key in ("request_id", "path", "status", "latency_ms", "error", "db_ms", "cache"):
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
# Exposes /metrics with a request counter and a duration histogram, both labelled by
# service so the agent's MetricErrorRatioDetector / MetricLatencyDetector can query a real
# Prometheus histogram instead of inferring p95 from log lines.
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


# serve /metrics outside Flask routing so scrapes aren't counted as application traffic
app.wsgi_app = _Dispatcher(app.wsgi_app, {"/metrics": _mwa()})

# ---- chaos state ----------------------------------------------------------
CHAOS = {"latency": False, "errors": False}
_leak = []  # holds allocated memory during the memleak scenario


def chaos_delay():
    if CHAOS["latency"]:
        time.sleep(random.uniform(2.0, 6.0))


def chaos_error():
    return CHAOS["errors"] and random.random() < 0.7


# ---- dependencies ---------------------------------------------------------
def get_redis():
    return redis_lib.Redis(host=REDIS_HOST, socket_connect_timeout=1, socket_timeout=1)


def db_query(sql, params=None, fetch=True):
    start = time.time()
    conn = psycopg2.connect(**DB_CONF)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall() if fetch else None
        return rows, round((time.time() - start) * 1000, 1)
    finally:
        conn.close()


# ---- request handling -----------------------------------------------------
def req_id():
    return request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])


def fail(rid, path, msg, error_code, status=500, exc=None):
    detail = f"{msg}: {exc}" if exc else msg
    log.error(detail, extra={"event": "request_failed", "request_id": rid, "path": path,
                             "status": status, "error": error_code})
    return jsonify({"error": error_code}), status


def verify_auth(rid, path):
    """Authenticate the caller via the auth service. Returns a failure response to return
    immediately, or None if authenticated. An auth outage surfaces as `auth_unreachable`."""
    try:
        # fail fast: a hung auth tier must not turn into user-facing latency, only into a
        # clean auth_unreachable error the agent can attribute to auth
        resp = requests.get(AUTH_URL + "/verify", headers={"X-Request-ID": rid}, timeout=0.5)
        if resp.status_code >= 500:
            return fail(rid, path, "auth service error", "auth_unreachable", 503)
        return None
    except requests.exceptions.RequestException as exc:
        return fail(rid, path, "cannot reach auth service", "auth_unreachable", 503, exc)


@app.route("/products")
def products():
    rid = req_id()
    start = time.time()
    denied = verify_auth(rid, "/products")
    if denied:
        return denied
    chaos_delay()
    if chaos_error():
        return fail(rid, "/products", "unhandled exception in product handler",
                    "internal_error")
    cache_hit = False
    try:
        r = get_redis()
        cached = r.get("products")
        if cached:
            cache_hit = True
            data = json.loads(cached)
            db_ms = 0
        else:
            rows, db_ms = db_query("SELECT id, name, price FROM products LIMIT 20")
            data = [{"id": a, "name": b, "price": float(c)} for a, b, c in rows]
            r.setex("products", 10, json.dumps(data))
    except redis_lib.exceptions.RedisError as exc:
        # degrade gracefully: redis down -> hit the db directly
        log.warning(f"redis unavailable, falling back to db: {exc}",
                    extra={"event": "cache_degraded", "request_id": rid, "error": "redis_unreachable"})
        try:
            rows, db_ms = db_query("SELECT id, name, price FROM products LIMIT 20")
            data = [{"id": a, "name": b, "price": float(c)} for a, b, c in rows]
        except psycopg2.OperationalError as exc2:
            return fail(rid, "/products", "database connection failed", "db_unreachable", 503, exc2)
    except psycopg2.OperationalError as exc:
        return fail(rid, "/products", "database connection failed", "db_unreachable", 503, exc)

    latency = round((time.time() - start) * 1000, 1)
    log.info("GET /products ok", extra={"event": "request", "request_id": rid, "path": "/products",
                                        "status": 200, "latency_ms": latency, "db_ms": db_ms,
                                        "cache": "hit" if cache_hit else "miss"})
    return jsonify({"products": data})


@app.route("/orders", methods=["POST"])
def orders():
    rid = req_id()
    start = time.time()
    denied = verify_auth(rid, "/orders")
    if denied:
        return denied
    chaos_delay()
    if chaos_error():
        return fail(rid, "/orders", "failed to process order: payment validation error",
                    "internal_error")
    item = (request.json or {}).get("item", "widget")
    try:
        _, db_ms = db_query("INSERT INTO orders (item, status) VALUES (%s, 'pending')",
                            (item,), fetch=False)
        try:
            get_redis().rpush("jobs", json.dumps({"type": "fulfill_order", "item": item, "request_id": rid}))
        except redis_lib.exceptions.RedisError as exc:
            log.error(f"order saved but job enqueue failed: {exc}",
                      extra={"event": "enqueue_failed", "request_id": rid, "error": "redis_unreachable"})
    except psycopg2.OperationalError as exc:
        return fail(rid, "/orders", "database connection failed", "db_unreachable", 503, exc)

    latency = round((time.time() - start) * 1000, 1)
    log.info(f"POST /orders ok item={item}", extra={"event": "request", "request_id": rid,
                                                    "path": "/orders", "status": 201,
                                                    "latency_ms": latency, "db_ms": db_ms})
    return jsonify({"status": "created", "item": item}), 201


@app.route("/users/lookup")
def users_lookup():
    rid = req_id()
    start = time.time()
    denied = verify_auth(rid, "/users/lookup")
    if denied:
        return denied
    chaos_delay()
    if chaos_error():
        return fail(rid, "/users/lookup", "user lookup failed: session store corrupt",
                    "internal_error")
    try:
        rows, db_ms = db_query("SELECT id, username FROM users ORDER BY random() LIMIT 1")
    except psycopg2.OperationalError as exc:
        return fail(rid, "/users/lookup", "database connection failed", "db_unreachable", 503, exc)
    latency = round((time.time() - start) * 1000, 1)
    log.info("GET /users/lookup ok", extra={"event": "request", "request_id": rid,
                                            "path": "/users/lookup", "status": 200,
                                            "latency_ms": latency, "db_ms": db_ms})
    user = {"id": rows[0][0], "username": rows[0][1]} if rows else None
    return jsonify({"user": user})


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "service": SERVICE})


# ---- chaos control --------------------------------------------------------
@app.route("/chaos/<scenario>/<action>", methods=["POST"])
def chaos(scenario, action):
    if scenario in CHAOS and action in ("on", "off"):
        CHAOS[scenario] = action == "on"
        log.warning(f"chaos scenario '{scenario}' turned {action}",
                    extra={"event": "chaos_toggle"})
        return jsonify({"scenario": scenario, "state": action})

    if scenario == "memleak" and action == "start":
        def leak():
            log.warning("memory leak scenario started", extra={"event": "chaos_toggle"})
            while True:
                _leak.append(bytearray(8 * 1024 * 1024))  # +8MB per tick
                if len(_leak) % 4 == 0:
                    log.warning(f"memory usage growing: ~{len(_leak) * 8}MB allocated",
                                extra={"event": "memory_pressure"})
                time.sleep(1.5)
        threading.Thread(target=leak, daemon=True).start()
        return jsonify({"scenario": "memleak", "state": "started"})

    return jsonify({"error": "unknown scenario"}), 404


if __name__ == "__main__":
    log.info("api starting up", extra={"event": "startup"})
    app.run(host="0.0.0.0", port=5000, threaded=True)
