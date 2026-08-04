#!/usr/bin/env bash
set -euo pipefail

echo "==> ruff format --check"
uv run ruff format --check .

echo "==> ruff check"
uv run ruff check .

echo "==> mypy"
uv run mypy

echo "==> pytest"
uv run pytest -q

echo "==> secret scan"
./scripts/check-secrets.sh

echo "==> dependency audit"
./scripts/check-deps.sh

echo "ALL GATES PASSED"
