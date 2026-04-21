# justfile for jupyter-loopback

set dotenv-load := false
set shell := ["bash", "-euo", "pipefail", "-c"]

# --- Variables ---
docker_image := env("DOCKER_IMAGE", "jupyter-loopback-demo")
port         := env("PORT", "8888")

# Default: list recipes
default:
    @just --list

# Install dev dependencies and pre-commit hooks
install:
    pip install -e ".[dev]"
    pre-commit install

# Run all pre-commit hooks (Python) and biome check (JS); both always run
lint:
    #!/usr/bin/env bash
    set -uo pipefail
    rc=0
    pre-commit run --all-files || rc=$?
    npx --yes -p @biomejs/biome@2.2.4 biome check jupyter_loopback/static || rc=$?
    exit $rc

# Auto-format Python (pre-commit also formats on commit)
format:
    ruff format jupyter_loopback tests demos
    ruff check --fix jupyter_loopback tests demos

# Strict mypy (Python) and tsc --noEmit (JS); both always run
typecheck:
    #!/usr/bin/env bash
    set -uo pipefail
    rc=0
    mypy jupyter_loopback || rc=$?
    npx --yes -p typescript@5.6.3 tsc --noEmit -p tsconfig.json || rc=$?
    exit $rc

# JSDoc type check of the widget bundle via the TypeScript compiler
typecheck-js:
    npx --yes -p typescript@5.6.3 tsc --noEmit -p tsconfig.json

# Biome lint + format check of the JS bundle
lint-js:
    npx --yes -p @biomejs/biome@2.2.4 biome check jupyter_loopback/static

# Run tests
test:
    pytest

# Run tests with coverage (requires pytest-cov)
coverage:
    pytest --cov=jupyter_loopback --cov-report=term-missing

# Build sdist + wheel into ./dist
build:
    python -m build

# --- Docker demo ---

# Build the demo image
docker:
    docker build -t {{ docker_image }} .

# Run the demo image on localhost:{{port}}
docker-run: docker
    docker run --rm -it -p {{ port }}:8888 {{ docker_image }}

# --- Cleanup ---

# Remove build artifacts
clean:
    rm -rf dist/ build/ *.egg-info
    rm -rf htmlcov/ .coverage
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
