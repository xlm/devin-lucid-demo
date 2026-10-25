#!/usr/bin/env bash
# Integration tier only: start the single-node Redpanda broker.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --wait redpanda
docker compose exec -T redpanda rpk cluster info
