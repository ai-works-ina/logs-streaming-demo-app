"""worker: background job processor. Pops jobs from the Redis queue and 'fulfills' them."""
import json
import logging
import os
import random
import sys
import time

import psycopg2
import redis as redis_lib
import requests

SERVICE = os.environ.get("SERVICE_NAME", "worker")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:5000")
DB_CONF = dict(
    host=os.environ.get("DB_HOST", "postgres"),
    user=os.environ.get("DB_USER", "demo"),
    password=os.environ.get("DB_PASSWORD", "demo"),
    dbname=os.environ.get("DB_NAME", "demo"),
    connect_timeout=2,
)


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
        for key in ("request_id", "job_type", "latency_ms", "error", "queue_depth"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.addHandler(handler)

# ---- prometheus metrics (added for the SRE agent's MetricSource path) -----------
# The worker has no HTTP server, so expose a metrics endpoint of its own. queue_depth feeds
# the agent's SaturationDetector (USE); jobs_processed_total tracks throughput by outcome.
from prometheus_client import start_http_server as _start_metrics, Gauge as _Gauge, Counter as _Counter

QUEUE_DEPTH = _Gauge("queue_depth", "Redis job-queue depth observed by the worker")
JOBS_DONE = _Counter("jobs_processed_total", "Jobs the worker handled", ["status"])


def main():
    log.info("worker starting up", extra={"event": "startup"})
    _start_metrics(5000)   # Prometheus scrapes worker:5000/metrics
    r = redis_lib.Redis(host=REDIS_HOST, socket_connect_timeout=2)
    last_heartbeat = 0.0

    while True:
        try:
            # heartbeat with queue depth every ~15s
            if time.time() - last_heartbeat > 15:
                depth = r.llen("jobs")
                QUEUE_DEPTH.set(depth)
                level = logging.WARNING if depth > 50 else logging.INFO
                log.log(level, f"heartbeat, queue depth={depth}",
                        extra={"event": "heartbeat", "queue_depth": depth})
                last_heartbeat = time.time()

            item = r.blpop("jobs", timeout=5)
            if not item:
                continue

            job = json.loads(item[1])
            start = time.time()
            time.sleep(random.uniform(0.1, 0.6))  # simulate work

            # charge the order through the payments provider before fulfilling it
            try:
                resp = requests.post(PAYMENTS_URL + "/charge",
                                     json={"request_id": job.get("request_id")}, timeout=1)
                if resp.status_code >= 500:
                    raise requests.exceptions.RequestException(f"HTTP {resp.status_code}")
            except requests.exceptions.RequestException as exc:
                log.error(f"job processing failed, payments unreachable: {exc}",
                          extra={"event": "job_failed", "job_type": job.get("type"),
                                 "request_id": job.get("request_id"),
                                 "error": "payments_unreachable"})
                JOBS_DONE.labels("failed").inc()
                r.rpush("jobs", item[1])  # requeue
                time.sleep(1)
                continue

            # mark one pending order as fulfilled
            try:
                conn = psycopg2.connect(**DB_CONF)
                with conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE orders SET status='fulfilled' "
                        "WHERE id = (SELECT id FROM orders WHERE status='pending' "
                        "ORDER BY id LIMIT 1)")
                conn.close()
            except psycopg2.OperationalError as exc:
                log.error(f"job processing failed, database unreachable: {exc}",
                          extra={"event": "job_failed", "job_type": job.get("type"),
                                 "request_id": job.get("request_id"), "error": "db_unreachable"})
                JOBS_DONE.labels("failed").inc()
                r.rpush("jobs", item[1])  # requeue
                time.sleep(2)
                continue

            latency = round((time.time() - start) * 1000, 1)
            JOBS_DONE.labels("done").inc()
            log.info(f"processed job {job.get('type')}",
                     extra={"event": "job_done", "job_type": job.get("type"),
                            "request_id": job.get("request_id"), "latency_ms": latency})

        except redis_lib.exceptions.RedisError as exc:
            log.error(f"cannot reach redis, retrying in 3s: {exc}",
                      extra={"event": "redis_down", "error": "redis_unreachable"})
            time.sleep(3)
        except Exception as exc:  # keep the worker alive no matter what
            log.error(f"unexpected worker error: {exc}", extra={"event": "worker_error"})
            time.sleep(1)


if __name__ == "__main__":
    main()
