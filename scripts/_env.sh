# Sourced by scripts/*.sh: repo root, venv and PYTHONPATH.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY_BOOT="${PYTHON:-}"
if [[ -z "$PY_BOOT" ]]; then
  for c in /usr/bin/python3 python3; do command -v "$c" >/dev/null && { PY_BOOT="$c"; break; }; done
fi
if [[ ! -x .venv/bin/python ]]; then
  "$PY_BOOT" -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt
fi
PY="$ROOT/.venv/bin/python"
export PYTHONPATH="$ROOT:$ROOT/consumer-warranty/src${PYTHONPATH:+:$PYTHONPATH}"
