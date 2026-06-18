# SRE Demo System

A small but realistic "production" system that generates a continuous, live stream of
structured JSON logs — built to be broken on purpose. Your SRE agent (built later)
simply tails the aggregated log stream and detects/diagnoses the incidents you inject.

## Architecture

```
loadgen ──> gateway (nginx) ──> webapp ──> api ──> postgres
 (traffic)    (JSON access        |         |  └──> redis (cache + job queue)
               logs)              |         |              |
                                  |         |           worker (consumes jobs)
                                  └── every service logs JSON to stdout ──> docker logs
```

- **gateway** — nginx reverse proxy, JSON access logs
- **webapp** — front tier, calls api, logs timeouts/upstream failures
- **api** — core service using Postgres + Redis; hosts the chaos toggles
- **worker** — background job processor consuming a Redis queue, heartbeats with queue depth
- **postgres / redis** — real dependencies whose failure cascades realistically
- **loadgen** — constant synthetic traffic so logs always flow (no manual curling needed)

All logs are structured JSON on stdout, e.g.:

```json
{"ts":"2026-06-11T14:32:05.123Z","service":"api","level":"ERROR","event":"request_failed","message":"database connection failed: ...","request_id":"a1b2c3d4","path":"/products","status":503,"error":"db_unreachable"}
```

`request_id` is propagated gateway -> webapp -> api -> worker, so an agent can correlate
one user request across services.

## Quickstart

Requires Docker + Docker Compose.

```bash
docker compose up -d --build     # first start takes a minute (image builds)
./scripts/stream-logs.sh         # watch the live firehose
```

Sanity check: `curl http://localhost:8080/products`

## Observability stack (metrics + logs + traces)

The compose now also brings up a free, self-hosted **Grafana LGTM** stack so the agent can
detect on real metrics and traces, not just logs. Nothing here needs an account or API key —
it all runs on localhost.

| Component | What it stores | Where the agent reads it | Host port |
|-----------|----------------|--------------------------|-----------|
| **Prometheus** | RED/USE metrics scraped from each service's `/metrics` | PromQL `:9090/api/v1/query` | 9090 |
| **Loki** (+ **Alloy**) | the JSON logs, shipped from the Docker socket | LogQL `:3100` | 3100 |
| **Tempo** (+ **OTel Collector**) | distributed traces (OTLP from the services) | TraceQL `:3200` | 3200 |
| **Grafana** | dashboards/Explore over all three | — (humans) | 3000 |
| **cAdvisor** | per-container CPU/memory (USE) | scraped by Prometheus | — |

- The Flask services (`api`, `webapp`, `auth`, `payments`) expose `http_requests_total` and
  `http_request_duration_seconds` on `/metrics`; the `worker` exposes `queue_depth` and
  `jobs_processed_total`.
- **Tracing is opt-in per container** via `OTEL_ENABLED=1` (set in compose). If the collector
  is down the services still boot — spans are simply dropped, never blocking the app.
- Quick checks once up: `curl localhost:9090/api/v1/targets` (all `up`),
  `curl localhost:3100/ready`, and Grafana at <http://localhost:3000> (anonymous admin).

## The log stream (your agent's input)

```bash
./scripts/stream-logs.sh --json            # pure JSON lines, one per log event
./scripts/stream-logs.sh --json | my_agent # how the agent will consume it later
```

## Breaking things (incident injection)

```bash
./chaos/chaos.sh kill-db      # 503 cascade: db_unreachable across api/webapp/worker
./chaos/chaos.sh kill-redis   # graceful degradation: cache_degraded warnings, slower reqs
./chaos/chaos.sh latency      # 2-6s latency in api -> 504 timeouts at webapp/gateway
./chaos/chaos.sh errors       # random 500s from api -> error-rate spike
./chaos/chaos.sh memleak      # api memory grows, OOM-killed in ~1 min, auto-restarts
./chaos/chaos.sh kill-worker  # queue depth climbs in worker-less heartbeats... (silent failure)

./chaos/chaos.sh restore-all  # heal everything
./chaos/chaos.sh status       # container states
```

Each scenario produces a distinct, recognizable log signature — good targets for your
agent's detection logic:

| Scenario     | Signature in the stream                                              |
|--------------|----------------------------------------------------------------------|
| kill-db      | `error:"db_unreachable"`, 503s in api + 502/504 ripple to webapp/gateway, worker `job_failed` + requeues |
| kill-redis   | `event:"cache_degraded"` WARNs, worker `redis_down`, latency rises (no cache) |
| latency      | `latency_ms` spikes, webapp `upstream_timeout` 504s                  |
| errors       | 500 rate jumps to ~70% on api while latency stays normal             |
| memleak      | `memory_pressure` WARNs, then api goes silent, docker restart, `startup` event |
| kill-worker  | worker logs stop, no more `job_done`; queue silently grows (the subtle one) |

## Client demo runbook (suggested)

1. Two tmux panes: left `./scripts/stream-logs.sh`, right your agent (later).
2. Let healthy traffic flow ~30s — point out normal request logs.
3. `./chaos/chaos.sh kill-db` — watch the cascade, agent flags root cause.
4. `./chaos/chaos.sh restore-db` — recovery detected.
5. Finish with `memleak` — the most dramatic: pressure warnings, OOM, auto-restart.

## Teardown

```bash
docker compose down -v
```
