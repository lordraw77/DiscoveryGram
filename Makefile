# DiscoveryGram — development and release tasks.
.DEFAULT_GOAL := help
.PHONY: help version install lock lint format typecheck test test-live check run check-env audit \
        verify-contract docker/build docker/run docker/stop docker/logs docker/shell \
        clean release

IMAGE       ?= lordraw/discoverygram
UV          ?= uv
COMPOSE     ?= docker compose

# One source of truth for the version: the git tag, derived by the same code the
# build backend uses. `:=` so the shell runs once per make invocation, not once
# per reference.
VERSION     := $(shell $(UV) run --quiet python scripts/version.py 2>/dev/null || echo 0.0.0+unknown)
# Docker tags may not contain `+`, which PEP 440 local versions always do.
TAG         ?= $(subst +,-,$(VERSION))

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_/-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

version: ## Print the version derived from the git tag
	@echo $(VERSION)

install: ## Install all dependencies including dev tools
	$(UV) sync --all-extras

lock: ## Refresh uv.lock
	$(UV) lock

lint: ## Run ruff checks
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Auto-format and fix what ruff can fix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## Run mypy in strict mode
	$(UV) run mypy

test: ## Run the test suite (excludes live tests)
	$(UV) run pytest -m "not live" --cov --cov-report=term-missing

test-live: ## Run tests against a real NoteDiscovery instance (needs .env)
	$(UV) run pytest -m live

audit: ## Check installed dependencies against the advisory database (needs network)
	$(UV) run --with pip-audit pip-audit --strict

check: lint typecheck test ## Everything CI runs

run: ## Run the bot locally
	$(UV) run python -m discoverygram

check-env: ## Validate .env and print a redacted summary (no network)
	$(UV) run python scripts/check_env.py

verify-contract: ## Probe the live instance for the two unresolved behaviours
	$(UV) run python scripts/verify_contract.py

docker/build: ## Build the container image, tagged with the git-derived version
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

docker/run: ## Start the stack in the background
	VERSION=$(VERSION) $(COMPOSE) up -d --build

docker/stop: ## Stop and remove the stack
	$(COMPOSE) down

docker/logs: ## Follow container logs
	$(COMPOSE) logs -f discoverygram

docker/shell: ## Open a shell in the running container
	$(COMPOSE) exec discoverygram /bin/sh

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

release: check docker/build ## Verify everything, then build the release image
	@echo "Built $(IMAGE):$(TAG) (version $(VERSION))."
	@case "$(VERSION)" in \
	  *dev*|*+*|0.0.0*) \
	    echo "WARNING: this is not a release version. Tag the commit first:"; \
	    echo "         git tag -a vX.Y.Z -m 'X.Y.Z' && make release";; \
	  *) echo "Push it with: docker push $(IMAGE):$(TAG)";; \
	esac
