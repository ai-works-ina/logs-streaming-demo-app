#!/usr/bin/env bash
# chaos.sh - break the system on purpose. Run from the project root.
# Usage: ./chaos/chaos.sh <scenario>
set -euo pipefail

API="http://localhost:8080"   # not used for chaos toggles; those go via docker exec

usage() {
  cat <<EOF
Usage: ./chaos/chaos.sh <scenario>

Incident scenarios:
  kill-db        Stop Postgres            -> 503s, db_unreachable errors cascade
  kill-redis     Stop Redis               -> cache_degraded warnings, worker errors
  latency        Inject 2-6s API latency  -> gateway/webapp timeouts (504s)
  errors         Inject random API 500s   -> error-rate spike
  memleak        Start memory leak in api -> OOM kill + container restart (~1 min)
  kill-worker    Stop the worker          -> redis queue depth grows

Recovery:
  heal           Stop latency/error injection
  restore-db     Start Postgres again
  restore-redis  Start Redis again
  restore-worker Start the worker again
  restore-all    Heal everything

  status         Show container states
EOF
  exit 1
}

chaos_toggle() {  # scenario, action — toggle inside the api container
  docker exec api python -c "
import urllib.request
req = urllib.request.Request('http://localhost:5000/chaos/$1/$2', method='POST')
print(urllib.request.urlopen(req).read().decode())"
}

case "${1:-}" in
  kill-db)        echo '>>> Stopping Postgres...';        docker stop postgres ;;
  kill-redis)     echo '>>> Stopping Redis...';           docker stop redis ;;
  kill-worker)    echo '>>> Stopping worker...';          docker stop worker ;;
  latency)        echo '>>> Injecting latency into api...';  chaos_toggle latency on ;;
  errors)         echo '>>> Injecting 500 errors into api...'; chaos_toggle errors on ;;
  memleak)        echo '>>> Starting memory leak in api (OOM in ~1 min)...'; chaos_toggle memleak start ;;
  heal)           echo '>>> Healing latency/errors...';   chaos_toggle latency off; chaos_toggle errors off ;;
  restore-db)     echo '>>> Restarting Postgres...';      docker start postgres ;;
  restore-redis)  echo '>>> Restarting Redis...';         docker start redis ;;
  restore-worker) echo '>>> Restarting worker...';        docker start worker ;;
  restore-all)
    docker start postgres redis worker 2>/dev/null || true
    chaos_toggle latency off || true
    chaos_toggle errors off || true
    echo '>>> All restored.' ;;
  status)         docker compose ps ;;
  *)              usage ;;
esac
