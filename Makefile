.PHONY: setup lock run dev up down migrate test test-pg lint security clean start-local kill-local

# Setup — installs runtime + dev deps from pyproject.toml via the uv.lock (single source of truth)
setup:
	uv sync
	uv run pre-commit install

# Regenerate the lockfile + the GENERATED requirements*.txt consumed by Docker/Railway.
# Run this after editing dependencies in pyproject.toml — the ONLY place deps are declared by hand.
lock:
	uv lock
	uv export --no-hashes --no-default-groups --no-emit-project --format requirements-txt -o requirements.txt
	uv export --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt

# Run
run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Docker (infra only — db, minio, redis; use start-local to run the backend without Docker)
up:
	docker compose up -d db minio redis

down:
	docker compose down

# Local dev without Docker (PowerShell only — opens backend + frontend in new windows)
start-local:
	powershell -File scripts/start-local.ps1

kill-local:
	powershell -File scripts/kill-local.ps1

# Database
migrate:
	uv run alembic upgrade head

migration:
	uv run alembic revision --autogenerate -m "$(msg)"

# Testing
test:
	uv run pytest -v --tb=short

# Run the suite against real PostgreSQL, in Linux containers (see scripts/test-postgres.sh).
# Pass args through: make test-pg ARGS="tests/test_auth_service.py -q"
test-pg:
	bash scripts/test-postgres.sh $(ARGS)

test-cov:
	uv run pytest --cov=app --cov-report=html --cov-report=term-missing

# Report-only storage orphan audit (never deletes) — see scripts/audit_storage_orphans.py
audit-storage:
	uv run python -m scripts.audit_storage_orphans --scope all

# Linting
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy app/

format:
	uv run ruff check --fix .
	uv run ruff format .

# Security
security:
	uv run bandit -r app/ -ll
	uv run pip-audit

# Clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache htmlcov .coverage
