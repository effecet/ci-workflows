# ci-workflows — local convenience targets.
#
# Most targets require CODEBERG_TOKEN in the environment, e.g.:
#   export CODEBERG_TOKEN=<your Forgejo/Codeberg API token>
#
# Set VERSION on commands that need it (the central tier tag to render):
#   make pilot VERSION=v1
#   make fanout VERSION=v1

VERSION ?= v1
# `uv run` uses the project venv; a bare system python3 may lack the package
# (ModuleNotFoundError on `make dry-run`). `--extra dev`
# pulls pytest + ruff, which `uv run` skips by default.
PY ?= uv run --extra dev python
# soak + fanout run for ~1h+; on macOS keep the host awake for their duration
# (a sleep mid-run freezes the poll loop). Empty elsewhere.
CAFFEINATE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)
# Safe default for full-fleet fanouts — some hosts silently throttle bursts.
# Override to 0 for fast fanouts you're confident won't trip the limit.
RATE_LIMIT_COOLDOWN ?= 360

.PHONY: help test lint dry-run pilot soak fanout drift drift-badge provision-secrets clean

help:
	@echo "Targets:"
	@echo "  test              pytest"
	@echo "  lint              ruff check + ruff format --check + actionlint"
	@echo "  dry-run           sync.py --dry-run --caller-filter=all   (VERSION=...)"
	@echo "  pilot             sync.py --caller-filter=example-org/example-app   (VERSION=...)"
	@echo "  soak              sync.py --soak-check=example-org/example-app --min-runs=3"
	@echo "  fanout            sync.py --caller-filter=all --exclude=example-org/example-app"
	@echo "                    (VERSION=...; RATE_LIMIT_COOLDOWN=360 by default, 0 for fast)"
	@echo "  drift             drift.py --mode=check   (VERSION=...)"
	@echo "  drift-badge       drift.py --mode=badge --readme=README.md   (VERSION=...)"
	@echo "  provision-secrets provision_secrets.py"
	@echo "  clean             remove caches"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	@find .github/workflows .forgejo/workflows -name '*.yml' 2>/dev/null | xargs -r actionlint -config-file .github/actionlint.yaml

dry-run:
	$(PY) -m ci_workflows.sync --dry-run --caller-filter=all --version=$(VERSION)

pilot:
	$(PY) -m ci_workflows.sync --caller-filter=example-org/example-app --version=$(VERSION)

soak:
	$(CAFFEINATE) $(PY) -u -m ci_workflows.sync --soak-check=example-org/example-app --min-runs=3

fanout:
	$(CAFFEINATE) $(PY) -u -m ci_workflows.sync --caller-filter=all --exclude=example-org/example-app --version=$(VERSION) --rate-limit-cooldown=$(RATE_LIMIT_COOLDOWN)

drift:
	$(PY) -m ci_workflows.drift --mode=check --version=$(VERSION)

drift-badge:
	$(PY) -m ci_workflows.drift --mode=badge --version=$(VERSION) --readme=README.md

provision-secrets:
	$(PY) -m ci_workflows.provision_secrets

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ src/**/__pycache__ tests/**/__pycache__
