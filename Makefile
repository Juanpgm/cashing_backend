.PHONY: setup run dev up down migrate test lint security clean start-local kill-local

# Setup
setup:
	uv venv
	uv pip install -r requirements-dev.txt
	uv run pre-commit install

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

test-cov:
	uv run pytest --cov=app --cov-report=html --cov-report=term-missing

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
