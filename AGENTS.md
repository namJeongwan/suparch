# Repository workflow

- Never commit or push directly to `main`.
- Create a feature, fix, or chore branch before changing tracked files.
- Run `uv run pytest` and `uv run ruff check .` before pushing.
- Keep the MCP runtime read-only; catalog mutation belongs in offline tooling.
