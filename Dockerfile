FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    SUPARCH_TRANSPORT="streamable-http" \
    SUPARCH_HOST="0.0.0.0" \
    SUPARCH_PORT="8000"

EXPOSE 8000

CMD ["suparch"]
