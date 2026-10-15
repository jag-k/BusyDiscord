FROM ghcr.io/astral-sh/uv:0.12.21-python3.14-trixie-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# SIGINT lets asyncio.run cancel tasks and clear the device before exiting.
STOPSIGNAL SIGINT
ENTRYPOINT ["/app/.venv/bin/busydiscord"]
