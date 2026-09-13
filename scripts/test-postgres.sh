#!/usr/bin/env bash
# Run the test suite against a real PostgreSQL, entirely in Linux containers.
#
# Why containers: on a Windows host, asyncpg cannot talk to the dockerized Postgres
# (Docker Desktop/WSL2 resets the PG wire protocol). Running pytest inside the `app`
# service (Linux) against `db:5432` sidesteps that — Linux client, Linux server.
#
# Infra topology (post infra/backend split): Postgres lives in the INDEPENDENT
# `cashing-infra` stack (docker-compose.infra.yml); the `app` service lives in the
# separate `cashing-backend` stack (docker-compose.yml, this project's default
# compose file — it has no `db` service of its own anymore). Both stacks join the
# shared EXTERNAL network `cashing-net`, so `app` reaches Postgres at `db:5432`
# purely over that internal network — the host port publish in
# docker-compose.infra.yml (5432:5432) is never used by this script and is NOT
# required for the suite to pass. docker-compose.infra.test.yml (committed
# alongside this script) resets that publish to empty for the `up`/`exec` calls
# this script makes, so a host port 5432 already held by an unrelated project's
# own Postgres container never blocks this script.
#
# Usage:
#   scripts/test-postgres.sh                 # whole suite
#   scripts/test-postgres.sh tests/test_auth_service.py -q   # any pytest args
#   DOWN=1 scripts/test-postgres.sh          # stop the db container when done
#
# The app image carries no tests (see .dockerignore); the suite is provided live via
# the compose volume mount, so nothing test-related is ever baked into an image.
set -euo pipefail

cd "$(dirname "$0")/.."

# `db` lives in the separate `cashing-infra` stack, not in this project's default
# docker-compose.yml — target it explicitly, with the test-only port override.
INFRA_COMPOSE=(docker compose -f docker-compose.infra.yml -f docker-compose.infra.test.yml)

# Tests use a dedicated database so they never collide with the running app (which
# uses `cashin`). The suite drops/recreates its schema every test.
DB_URL="postgresql+asyncpg://cashin:cashin_local@db:5432/cashin_test"
PYTEST_ARGS=("$@")
if [ ${#PYTEST_ARGS[@]} -eq 0 ]; then
  PYTEST_ARGS=(-q --tb=short -ra)
fi

# Both the infra stack and the backend stack join this shared external network —
# create it if this is a fresh checkout/worktree with no infra ever started
# (mirrors scripts/up-infra.ps1's own idempotent creation).
if [ -z "$(docker network ls --filter 'name=^cashing-net$' --format '{{.Name}}')" ]; then
  echo ">> creating shared network cashing-net"
  docker network create cashing-net >/dev/null
fi

echo ">> starting Postgres (db) ..."
"${INFRA_COMPOSE[@]}" up -d --wait db
# `docker compose run` waits on the depends_on healthcheck, but start it eagerly so
# a cold pull/boot doesn't count against the test run.

echo ">> ensuring test database cashin_test exists"
"${INFRA_COMPOSE[@]}" exec -T db psql -U cashin -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='cashin_test'" | grep -q 1 \
  || "${INFRA_COMPOSE[@]}" exec -T db psql -U cashin -d postgres -c "CREATE DATABASE cashin_test"

echo ">> running suite in the app container against $DB_URL"
set +e
# S3_ENDPOINT_URL is set on the `app` service so the *running* app uses MinIO, but the
# storage tests use moto (in-process mock) and must NOT be pointed at real MinIO — clear
# it for the test run so moto intercepts as designed.
#
# `app`'s docker-compose.yml env_file (.env) is `required: false` — this script (and
# any fresh worktree without a local .env) works without one; Settings
# (app/core/config.py) already tolerates a missing .env via its own env_file tuple
# and safe defaults for every field, so nothing here needs a real secret to boot.
docker compose run --rm \
  -e TEST_DATABASE_URL="$DB_URL" \
  -e S3_ENDPOINT_URL= \
  app python -m pytest "${PYTEST_ARGS[@]}"
code=$?
set -e

if [ "${DOWN:-0}" = "1" ]; then
  echo ">> tearing down containers"
  "${INFRA_COMPOSE[@]}" down
fi

exit "$code"
