# Minimal image for running the Telegram bot (Python only, MySQL is external)

# Build stage: install locked runtime dependencies into /opt/venv with uv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# uv.lock is the single source of truth; --frozen fails instead of re-resolving
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Runtime stage: only the virtualenv and application code
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY modules ./modules
COPY resources ./resources
COPY .env.example ./.env.example

# Expose no ports; the bot connects out to Telegram
CMD ["python", "-u", "modules/main.py"]
