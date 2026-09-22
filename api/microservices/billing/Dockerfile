# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY src/ ./src/
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

# Test stage. Runs the offline suite against an injected in-memory aiosqlite engine — no
# Postgres needed. Tests are the canonical copy under api/tests/billing/ (project.md); they
# are mounted at run, not copied in, so there is one source of truth. httpx is test-only
# (fastapi.testclient.TestClient imports it), same category as pytest — NOT a runtime dep.
#   docker build --target test -t billing-test .
#   docker run --rm -v "$(pwd)/../../tests/billing/tests:/app/tests" billing-test
FROM base AS test
RUN pip install pytest pytest-asyncio aiosqlite httpx
RUN printf '[pytest]\npythonpath = src\ntestpaths = tests\nasyncio_mode = auto\n' > pytest.ini
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image. The entrypoint applies Alembic
# migrations (the ONLY place production Postgres DDL is created) then serves.
FROM base AS runtime
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
WORKDIR /app/src
EXPOSE 8000
CMD ["/app/docker-entrypoint.sh"]
