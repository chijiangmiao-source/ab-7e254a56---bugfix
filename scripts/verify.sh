#!/usr/bin/env bash
# One-shot acceptance gate:
#   1. backend code tests (pytest)
#   2. build the web page (vite)
#   3. boot a real uvicorn server and smoke-test power-loss recovery and
#      concurrent upgrade adjudication over HTTP
# Exits non-zero if any stage fails.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SMOKE_PORT="${SMOKE_PORT:-18080}"
TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"; [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo "==> [1/4] backend unit/integration tests"
if [ -x ".venv/bin/python" ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
"$PY" -m pytest -q tests

echo "==> [2/4] build web page"
cd "$ROOT/web"
if [ ! -d node_modules ]; then
  npm install --no-audit --no-fund
fi
npm run build
test -f "$ROOT/web/dist/index.html"
cd "$ROOT"

echo "==> [3/4] start real HTTP server on :$SMOKE_PORT"
DATA_PATH="$TMPDIR_RUN/upgrade.db" "$PY" -m uvicorn backend.api:app \
  --host 127.0.0.1 --port "$SMOKE_PORT" --log-level warning &
SERVER_PID=$!

for i in $(seq 1 50); do
  if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$SMOKE_PORT/api/health', timeout=1)" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited early" >&2
    exit 1
  fi
  sleep 0.3
done

echo "==> [4/4] HTTP smoke: power-loss recovery + concurrent adjudication"
"$PY" scripts/smoke_http.py "http://127.0.0.1:$SMOKE_PORT"

echo "==> VERIFY PASSED"
