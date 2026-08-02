#!/usr/bin/env bash
# Run the test suite against a real PostgreSQL, entirely in Linux containers.
#
# Why containers: on a Windows host, asyncpg cannot talk to the dockerized Postgres
# (Docker Desktop/WSL2 resets the PG wire protocol). Running pytest inside the `app`
# service (Linux) against `db:5432` sidesteps that — Linux client, Linux server.
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

# Tests use a dedicated database so they never collide with the running app (which
# uses `cashin`). The suite drops/recreates its schema every test.
DB_URL="postgresql+asyncpg://cashin:cashin_local@db:5432/cashin_test"
PYTEST_ARGS=("$@")
if [ ${#PYTEST_ARGS[@]} -eq 0 ]; then
  PYTEST_ARGS=(-q --tb=short -ra)
fi

echo ">> starting Postgres (db) ..."
docker compose up -d --wait db
# `docker compose run` waits on the depends_on healthcheck, but start it eagerly so
# a cold pull/boot doesn't count against the test run.

echo ">> ensuring test database cashin_test exists"
docker compose exec -T db psql -U cashin -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='cashin_test'" | grep -q 1 \
  || docker compose exec -T db psql -U cashin -d postgres -c "CREATE DATABASE cashin_test"

echo ">> running suite in the app container against $DB_URL"
set +e
# S3_ENDPOINT_URL is set on the `app` service so the *running* app uses MinIO, but the
# storage tests use moto (in-process mock) and must NOT be pointed at real MinIO — clear
# it for the test run so moto intercepts as designed.
docker compose run --rm \
  -e TEST_DATABASE_URL="$DB_URL" \
  -e S3_ENDPOINT_URL= \
  app python -m pytest "${PYTEST_ARGS[@]}"
code=$?
set -e

if [ "${DOWN:-0}" = "1" ]; then
  echo ">> tearing down containers"
  docker compose down
fi

exit "$code"
