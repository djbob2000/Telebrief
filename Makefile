# Makefile for Telebrief

.PHONY: help install install-dev lint format clean run pre-commit check

help:
	@echo "Available commands:"
	@echo "  make install      - Install production dependencies"
	@echo "  make install-dev  - Install development dependencies"
	@echo "  make lint         - Run Ruff and MyPy checks"
	@echo "  make format       - Format code with Ruff"
	@echo "  make clean        - Remove build artifacts"
	@echo "  make run          - Run the application"
	@echo "  make pre-commit   - Install pre-commit hooks"

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt
	pre-commit install

lint:
	@echo "Running Ruff linter..."
	ruff check src
	@echo "\nRunning Ruff format check..."
	ruff format --check src
	@echo "\nRunning MyPy..."
	mypy src

format:
	ruff format src
	ruff check --fix src

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
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

check: lint
	@echo "All checks passed!"
