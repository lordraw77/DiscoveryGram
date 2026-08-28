# DiscoveryGram — development and release tasks.
.DEFAULT_GOAL := help
.PHONY: help version install lock lint format typecheck test test-live check run check-env audit \
        verify-contract docker/build docker/buildx docker/push docker/pins \
        docker/run docker/stop docker/logs docker/shell clean release

IMAGE       ?= lordraw/discoverygram
UV          ?= uv
COMPOSE     ?= docker compose
# The architectures the release image is published for. QEMU emulates whatever
# the build host is not, so adding one here costs build minutes, not code.
PLATFORMS   ?= linux/amd64,linux/arm64
EXPORTED_REQS := .requirements-audit.txt
BASE_IMAGES := ghcr.io/astral-sh/uv:python3.12-bookworm-slim python:3.12-slim-bookworm

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

audit: ## Check locked dependencies against the advisory database (needs network)
	# Audit the exported lockfile, not the environment: the environment contains
	# discoverygram itself, which is editable and not on PyPI, and `--strict`
	# turns both of those into an error rather than a skip.
	$(UV) export --format requirements-txt --no-emit-project --no-hashes --all-extras \
		--quiet -o $(EXPORTED_REQS)
	$(UV) run --with pip-audit pip-audit --strict -r $(EXPORTED_REQS)
	@rm -f $(EXPORTED_REQS)

check: lint typecheck test ## Everything CI runs

run: ## Run the bot locally
	$(UV) run python -m discoverygram

check-env: ## Validate .env and print a redacted summary (no network)
	$(UV) run python scripts/check_env.py

verify-contract: ## Probe the live instance for the two unresolved behaviours
	$(UV) run python scripts/verify_contract.py

docker/build: ## Build the container image, tagged with the git-derived version
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

docker/buildx: ## Build the multi-arch release image (does not push; see docker/push)
	# --load cannot hold a multi-arch result, so a local multi-arch build has
	# nowhere to put it but the cache. This target is here to prove the arm64
	# build compiles; docker/push is what produces a usable artefact.
	docker buildx build --platform $(PLATFORMS) \
		--build-arg VERSION=$(VERSION) -t $(IMAGE):$(TAG) .

docker/push: ## Build and push the multi-arch image under the git-derived tag
	@case "$(VERSION)" in \
	  *dev*|*+*|0.0.0*) \
	    echo "Refusing to push $(TAG): not a release version. Tag the commit first."; \
	    exit 1;; \
	esac
	docker buildx build --platform $(PLATFORMS) \
		--build-arg VERSION=$(VERSION) \
		-t $(IMAGE):$(TAG) -t $(IMAGE):latest --push .

docker/pins: ## Print the current digests of the base images, to refresh the Dockerfile
	@for img in $(BASE_IMAGES); do \
		printf '%s@%s\n' "$$img" "$$(docker buildx imagetools inspect $$img --format '{{.Manifest.Digest}}')"; \
	done

docker/run: ## Start the stack in the background
	VERSION=$(VERSION) $(COMPOSE) up -d --build

docker/stop: ## Stop and remove the stack
	$(COMPOSE) down

docker/logs: ## Follow container logs
	$(COMPOSE) logs -f discoverygram

docker/shell: ## Open a shell in the running container
	$(COMPOSE) exec discoverygram /bin/sh

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build $(EXPORTED_REQS)
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

release: check docker/build ## Verify everything, then build the release image
	@echo "Built $(IMAGE):$(TAG) (version $(VERSION))."
	@case "$(VERSION)" in \
	  *dev*|*+*|0.0.0*) \
	    echo "WARNING: this is not a release version. Tag the commit first:"; \
	    echo "         git tag -a vX.Y.Z -m 'X.Y.Z' && make release";; \
	  *) echo "Publish it with: make docker/push (multi-arch: $(PLATFORMS))";; \
	esac
