#!/usr/bin/env bash
# Integration tier only: stop the broker and drop its data.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v
