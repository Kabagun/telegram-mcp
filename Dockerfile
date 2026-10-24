FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app/data

USER 10001:10001

EXPOSE 8000

CMD ["/app/.venv/bin/telegram-ping-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
