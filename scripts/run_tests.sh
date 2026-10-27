#!/usr/bin/env bash
# Run the whole test suite. Picks a PostgreSQL in this order:
#   1) the one you already point at via PGHOST/PGPORT/PGUSER (libpq env vars)
#   2) docker compose (docker-compose.yml)
#   3) a temporary local cluster from installed PostgreSQL binaries
set -euo pipefail
cd "$(dirname "$0")/.."
cleanup() { :; }

if [[ -z "${PGHOST:-}" ]]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose up -d --wait db
    export PGHOST=localhost PGPORT=54320 PGUSER=postgres PGPASSWORD=test
    cleanup() { docker compose down -v >/dev/null 2>&1 || true; }
  else
    BIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)
    [[ -n "$BIN" ]] || { echo "Need docker, or PostgreSQL binaries, or PGHOST set." >&2; exit 1; }
    DATA=$(mktemp -d); SOCK=$(mktemp -d)
    RUN=(bash -c)
    if [[ $EUID -eq 0 ]]; then RUN=(su postgres -c); chown postgres "$DATA" "$SOCK"; fi
    chmod 700 "$DATA"
    "${RUN[@]}" "$BIN/initdb -D $DATA -A trust >/dev/null && $BIN/pg_ctl -D $DATA -o '-p 54330 -k $SOCK' -w start >/dev/null"
    export PGHOST=$SOCK PGPORT=54330 PGUSER=postgres
    cleanup() { "${RUN[@]}" "$BIN/pg_ctl -D $DATA -m fast stop >/dev/null" || true; rm -rf "$DATA" "$SOCK"; }
  fi
fi
trap cleanup EXIT

python3 -m pytest -v "$@"
