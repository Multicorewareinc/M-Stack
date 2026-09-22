# syntax=docker/dockerfile:1
#
# Build from the REPO ROOT, not from agentic/backend/ -- unlike
# service-template's fully self-contained services, this one depends on
# the sibling `multistack` SDK package, which has to be inside the build
# context:
#   docker build -f agentic/backend/Dockerfile --target test -t agentic-test . && docker run --rm agentic-test
#   docker build -f agentic/backend/Dockerfile -t agentic . && docker run --rm -p 8000:8000 agentic
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# The multistack SDK itself (zero third-party runtime deps -- see its own
# pyproject.toml), installed editable so mcp_server.py's
# `from multistack import ...` resolves.
COPY pyproject.toml README.md ./
COPY multistack/ ./multistack/
RUN pip install --upgrade pip && pip install -e .

# This layer's own dependencies.
COPY agentic/backend/requirements.txt ./agentic/backend/requirements.txt
RUN pip install -r agentic/backend/requirements.txt
COPY agentic/backend/src/ ./agentic/backend/src/

# Test stage: `docker build -f agentic/backend/Dockerfile --target test -t agentic-test . && docker run --rm agentic-test`
FROM base AS test
RUN pip install pytest pytest-asyncio
COPY agentic/backend/tests/ ./agentic/backend/tests/
COPY agentic/backend/pytest.ini ./agentic/backend/pytest.ini
WORKDIR /app/agentic/backend
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image.
FROM base AS runtime
WORKDIR /app/agentic/backend/src
EXPOSE 8000
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
