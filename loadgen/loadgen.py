"""loadgen: continuously sends realistic traffic to the gateway so logs always flow."""
import json
import logging
import os
import random
import sys
import time

import requests

SERVICE = "loadgen"
TARGET = os.environ.get("TARGET_URL", "http://gateway:80")
BASE_RPS = float(os.environ.get("BASE_RPS", "4"))


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
        for key in ("path", "status", "latency_ms"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.addHandler(handler)

# weighted traffic mix
ROUTES = [
    ("GET", "/products", 0.55),
    ("GET", "/profile", 0.25),
    ("POST", "/checkout", 0.15),
    ("GET", "/health", 0.05),
]
ITEMS = ["widget", "gadget", "sprocket", "flange", "gizmo", "doohickey"]


def pick_route():
    roll, acc = random.random(), 0.0
    for method, path, weight in ROUTES:
        acc += weight
        if roll <= acc:
            return method, path
    return ROUTES[0][0], ROUTES[0][1]


def main():
    log.info(f"loadgen starting, target={TARGET}, base_rps={BASE_RPS}",
             extra={"event": "startup"})
    time.sleep(5)  # let the stack come up
    while True:
        # diurnal-ish variation: rps oscillates around the base
        rps = max(0.5, BASE_RPS * random.uniform(0.6, 1.5))
        method, path = pick_route()
        start = time.time()
        try:
            if method == "POST":
                resp = requests.post(TARGET + path, json={"item": random.choice(ITEMS)}, timeout=8)
            else:
                resp = requests.get(TARGET + path, timeout=8)
            latency = round((time.time() - start) * 1000, 1)
            if resp.status_code >= 500:
                log.warning(f"{method} {path} -> {resp.status_code}",
                            extra={"event": "synthetic_request", "path": path,
                                   "status": resp.status_code, "latency_ms": latency})
        except requests.exceptions.RequestException as exc:
            latency = round((time.time() - start) * 1000, 1)
            log.error(f"{method} {path} failed: {type(exc).__name__}",
                      extra={"event": "synthetic_request_failed", "path": path,
                             "latency_ms": latency})
        time.sleep(1.0 / rps)


if __name__ == "__main__":
    main()
