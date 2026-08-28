# Makefile for Telebrief

.PHONY: help install install-dev test lint format clean run

help:
	@echo "Available commands:"
	@echo "  make install      - Install production dependencies"
	@echo "  make install-dev  - Install development dependencies"
	@echo "  make test         - Run tests with coverage"
	@echo "  make test-fast    - Run tests without coverage"
	@echo "  make lint         - Run linters (Ruff and MyPy)"
	@echo "  make format       - Format code with Ruff"
	@echo "  make clean        - Remove build artifacts"
	@echo "  make run          - Run the application"
	@echo "  make pre-commit   - Install pre-commit hooks"

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt
	pre-commit install

test:
	pytest --cov=src --cov-report=html --cov-report=term-missing -v

test-fast:
	pytest -v

test-unit:
	pytest -v -m unit

test-integration:
	pytest -v -m integration

lint:
	@echo "Running Ruff linter..."
	ruff check src tests
	@echo "\nRunning Ruff format check..."
	ruff format --check src tests
	@echo "\nRunning MyPy..."
	mypy src

format:
	ruff format src tests
	ruff check --fix src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	find . -type f -name '*.pyo' -delete
	find . -type f -name coverage.xml -delete
	find . -type f -name .coverage -delete
	rm -rf dist build *.egg-info

run:
	python main.py

backup:
	./scripts/backup_to_mega.sh

pre-commit:
	pre-commit install
	@echo "Pre-commit hooks installed!"

check: lint test
	@echo "All checks passed!"
