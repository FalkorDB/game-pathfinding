#!/usr/bin/env bash
# Start FalkorDB in Docker (falkordb/falkordb:v4.22.0) + serve the grid pathfinding demo.
#   ./run.sh                 # defaults: grid 30x30, DB :6379, app :8090
#   ./run.sh --n 50          # bigger grid -> more dramatic CCH struggle
#   ./run.sh stop            # stop the web app + remove the FalkorDB container
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

FALKOR_PORT="${FALKOR_PORT:-6379}"
APP_PORT="${PORT:-8090}"
GRID_N="${GRID_N:-30}"

# --- FalkorDB in Docker (self-contained; the CCH index ships in the image) ----------
IMAGE="${FALKORDB_IMAGE:-falkordb/falkordb:v4.22.0}"
CONTAINER="${CONTAINER:-falkordb-game-pathfinding}"
CPUS="${FALKORDB_CPUS:-8}"                  # generous — this demo is tiny, but be safe
MEMORY="${FALKORDB_MEMORY:-4g}"

usage() {
  cat <<EOF
Usage: ./run.sh [--n N] [stop] [-h|--help]

Start FalkorDB in Docker (falkordb/falkordb:v4.22.0) and serve the grid pathfinding
demo, then open http://localhost:${APP_PORT}.

Options:
  --n N        grid size, N x N cells (default ${GRID_N}). Bigger = more dramatic
               CCH struggle (e.g. 50 -> ~87ms rebuilds; 100 -> ~380ms).
  stop         stop the web app and remove the FalkorDB container
  -h, --help   show this help and exit

Environment overrides:
  FALKOR_PORT      FalkorDB host port  (default 6379)
  PORT             app port            (default 8090)
  FALKORDB_IMAGE   Docker image        (default falkordb/falkordb:v4.22.0)
  FALKORDB_CPUS    container --cpus    (default 8)
  FALKORDB_MEMORY  container --memory  (default 4g)
  VENV_PY          python with flask+falkordb (else a ./venv is created)

Examples:
  ./run.sh --n 50
  PORT=9000 ./run.sh --n 40
EOF
}

# ---- teardown -------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
  lsof -ti:"$APP_PORT" 2>/dev/null | xargs kill -9 2>/dev/null && echo "stopped web app" || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "removed $CONTAINER" || echo "no container"
  exit 0
fi

# ---- args (override env defaults) ----
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --n)       shift; GRID_N="${1:-}"; [ -n "${GRID_N}" ] || { echo "error: --n requires a value" >&2; exit 1; } ;;
    --n=*)     GRID_N="${1#*=}" ;;
    *)         echo "error: unknown option '$1'" >&2; echo >&2; usage >&2; exit 1 ;;
  esac
  shift
done
case "$GRID_N" in ''|*[!0-9]*) echo "error: --n must be a positive integer (got '$GRID_N')" >&2; exit 1 ;; esac
[ "$GRID_N" -ge 2 ] || { echo "error: --n must be >= 2 (got '$GRID_N')" >&2; exit 1; }

# ---- Python venv (self-contained: flask + falkordb) ----
VENV_PY="${VENV_PY:-$HERE/venv/bin/python}"
if [ ! -x "$VENV_PY" ]; then
  echo "[run] creating venv + installing deps (flask, falkordb) ..."
  PY="$(command -v python3.12 || command -v python3 || true)"
  [ -n "$PY" ] || { echo "error: python3 not found" >&2; exit 1; }
  "$PY" -m venv "$HERE/venv"
  "$HERE/venv/bin/pip" install -q --upgrade pip
  "$HERE/venv/bin/pip" install -q -r "$HERE/requirements.txt"
  VENV_PY="$HERE/venv/bin/python"
fi

# ---- FalkorDB (Docker) ----
# something already serving on the DB port (e.g. a container from another demo)? reuse it.
db_port_up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- 3<&-; }
if db_port_up "$FALKOR_PORT"; then
  echo "[falkordb] reusing FalkorDB already listening on :$FALKOR_PORT"
else
  command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found — install Docker Desktop" >&2; exit 1; }
  docker info      >/dev/null 2>&1 || { echo "FATAL: docker daemon not running — start Docker Desktop" >&2; exit 1; }
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "[falkordb] pulling $IMAGE ..."; docker pull "$IMAGE"; }
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "[falkordb] starting $IMAGE as '$CONTAINER' on :$FALKOR_PORT (cpus=$CPUS mem=$MEMORY)"
  docker run -d --rm --name "$CONTAINER" -p "127.0.0.1:$FALKOR_PORT:6379" \
    --cpus "$CPUS" --memory "$MEMORY" --memory-swap "$MEMORY" "$IMAGE" >/dev/null
  for _ in $(seq 1 120); do
    [ "$(docker exec "$CONTAINER" redis-cli ping 2>/dev/null)" = "PONG" ] && break
    sleep 0.5
  done
  echo "[falkordb] ready on :$FALKOR_PORT"
fi

# ---- serve (builds the grid + CCH on first boot) ----
echo "[run] app: http://localhost:${APP_PORT}   grid ${GRID_N}x${GRID_N}   (python: ${VENV_PY})"
FALKOR_PORT="$FALKOR_PORT" PORT="$APP_PORT" GRID_N="$GRID_N" exec "$VENV_PY" app.py
