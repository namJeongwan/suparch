FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/namJeongwan/suparch" \
      org.opencontainers.image.description="Structured supplement facts MCP server" \
      io.modelcontextprotocol.server.name="io.github.namjeongwan/suparch"

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    SUPARCH_TRANSPORT="streamable-http" \
    SUPARCH_HOST="0.0.0.0" \
    SUPARCH_PORT="8000" \
    SUPARCH_CATALOG_POINTER_URL="https://raw.githubusercontent.com/namJeongwan/suparch/catalog/v3/catalog-pointer.json"

EXPOSE 8000

CMD ["suparch"]
