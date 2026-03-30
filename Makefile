.PHONY: setup run dev up down migrate test lint security clean

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

# Docker
up:
	docker compose -f deploy/docker/docker-compose.yml up -d

down:
	docker compose -f deploy/docker/docker-compose.yml down

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
