FROM python:3.12-slim

# Official uv install pattern (https://docs.astral.sh/uv/guides/integration/docker/)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first so they cache independently of app code.
# Requires pyproject.toml, uv.lock (committed) and .python-version (pinned 3.12).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:${PATH}"

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY mcp_server ./mcp_server
# Ops tooling runs against THIS database from inside the container — the
# terminal-batch repair (`scripts/sweep_terminal_batch_trades.py`, docs/03)
# needs the app's own engine + settings, and the host has no Python env.
COPY scripts ./scripts

# Non-root runtime user (created after dependency install and app COPY so
# ownership of /app/.venv stays root-managed at build time; appuser only reads).
RUN useradd --uid 1000 --create-home appuser
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
