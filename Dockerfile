FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV \
  UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  PYTHONUNBUFFERED=1 \
  PATH="/app/.venv/bin:$PATH"
COPY \
  README.md \
  pyproject.toml \
  uv.lock \
  ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
CMD ["arb"]
