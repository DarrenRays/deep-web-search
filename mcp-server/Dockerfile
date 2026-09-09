FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies come from the same pyproject.toml the tests use, so the image and
# the local test run never drift apart.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY mcp-server/*.py ./

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8765
CMD ["python", "server.py"]
