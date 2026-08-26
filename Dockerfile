# syntax=docker/dockerfile:1.7

# --- Builder ----------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The version comes from the git tag, but `.git` is deliberately not in the
# build context — so the caller passes the derived version in and hatch-vcs
# uses it verbatim instead of looking for a repository that is not there.
# `make docker/build` computes it with scripts/version.py; a bare
# `docker build` with no --build-arg yields an honest 0.0.0+unknown rather
# than a plausible-looking lie.
ARG VERSION=0.0.0+unknown
# Both spellings: hatch-vcs only forwards the distribution name to setuptools-scm
# in some versions, and without it the `_FOR_<DIST>` form is never consulted.
# This stage builds exactly one project, so the unnamed variable is unambiguous.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISCOVERYGRAM=${VERSION}

# Dependencies first: this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
# --locked: build strictly from the committed lockfile and fail if it is stale,
# rather than silently resolving a different dependency set.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src/ ./src/
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- Runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG VERSION=0.0.0+unknown
LABEL org.opencontainers.image.title="DiscoveryGram" \
      org.opencontainers.image.description="Telegram front-end for a NoteDiscovery vault" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/lordraw77/DiscoveryGram"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 1001 discoverygram \
    && useradd --system --uid 1001 --gid discoverygram --create-home discoverygram

WORKDIR /app

COPY --from=builder --chown=discoverygram:discoverygram /app/.venv /app/.venv
COPY --from=builder --chown=discoverygram:discoverygram /app/src /app/src

USER discoverygram

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "discoverygram"]
