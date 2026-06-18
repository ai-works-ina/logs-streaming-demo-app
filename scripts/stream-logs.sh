#!/usr/bin/env bash
# stream-logs.sh - the single firehose your SRE agent will consume.
# Aggregates all services into one stream of JSON log lines on stdout.
#
# Usage:
#   ./scripts/stream-logs.sh                     # human-readable tail (with container prefix)
#   ./scripts/stream-logs.sh --json              # raw JSON lines only (strip docker prefix)
#   ./scripts/stream-logs.sh --json > stream.log # pipe to file
#   ./scripts/stream-logs.sh --json | my_agent   # pipe straight into your agent later
set -euo pipefail

if [[ "${1:-}" == "--json" ]]; then
  # Strip the "container-name  | " prefix docker compose adds, leaving pure JSON lines.
  docker compose logs -f --no-color --no-log-prefix
else
  docker compose logs -f
fi
