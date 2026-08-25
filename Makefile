# DiscoveryGram — development and release tasks.
.DEFAULT_GOAL := help
.PHONY: help install lock lint format typecheck test test-live check run check-env \
        verify-contract docker/build docker/run docker/stop docker/logs docker/shell \
        clean release

IMAGE       ?= discoverygram
TAG         ?= latest
UV          ?= uv
COMPOSE     ?= docker compose

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_/-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

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

check: lint typecheck test ## Everything CI runs

run: ## Run the bot locally
	$(UV) run python -m discoverygram

check-env: ## Validate .env and print a redacted summary (no network)
	$(UV) run python scripts/check_env.py

verify-contract: ## Probe the live instance for the two unresolved behaviours
	$(UV) run python scripts/verify_contract.py

docker/build: ## Build the container image
	docker build -t $(IMAGE):$(TAG) .

docker/run: ## Start the stack in the background
	$(COMPOSE) up -d --build

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
	@echo "Built $(IMAGE):$(TAG). Tag and push it with your registry of choice."
