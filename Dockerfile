# Titularr: the read-only HTTP API by default (`titularr serve`), with the whole CLI
# available (`docker run <image> validate`, `run <name>`, `history`, ...).
# Built and published to ghcr.io/kyletsavage/titularr by .github/workflows/docker.yml.

# --- Build: install Titularr and exactly what uv.lock pins into /app/.venv ---
FROM python:3.12-slim-trixie AS build

# uv, pinned (releases: https://github.com/astral-sh/uv/releases).
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/

# Precompile bytecode for a faster start; copy files out of uv's cache (it's a cache
# mount, so links into it would break); use this image's Python, never download one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: rebuilt only when the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

# Then Titularr itself (hatchling needs the README and LICENSE that pyproject names).
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Run: the same Python, the venv, no uv or build files ---
FROM python:3.12-slim-trixie

# A non-root user. uid 1000 is usually the first user on a Linux host, so files it
# reads in the mounted config folder line up; compose's `user:` can change it.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin titularr

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TITULARR_CONFIG=/config/config.yaml

USER titularr
WORKDIR /config
EXPOSE 8778

# `docker ps` shows healthy/unhealthy from the API's health route (needs no key).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8778/api/health', timeout=4)"]

LABEL org.opencontainers.image.source="https://github.com/kyletsavage/titularr" \
      org.opencontainers.image.description="Titularr's read-only HTTP API and CLI" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENTRYPOINT ["titularr"]
CMD ["serve", "--host", "0.0.0.0"]
