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
# Postgres needed. Tests are the canonical copy under api/tests/organization-control-plane/
# (project.md); they are mounted at run, not copied in, so there is one source of truth.
#   docker build --target test -t org-cp-test .
#   docker run --rm -v "$(pwd)/../../tests/organization-control-plane/tests:/app/tests" org-cp-test
FROM base AS test
RUN pip install pytest pytest-asyncio aiosqlite httpx fakeredis
RUN printf '[pytest]\npythonpath = src\ntestpaths = tests\nasyncio_mode = auto\n' > pytest.ini
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image. The entrypoint applies Alembic
# migrations (the ONLY place production Postgres DDL is created — ADR-015a) then serves.
FROM base AS runtime
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
WORKDIR /app/src
EXPOSE 8000

# Runs as a non-root uid so the chart can set runAsNonRoot/runAsUser and
# clear Pod Security Standards "restricted". The uid is NUMERIC on purpose:
# kubelet can only verify runAsNonRoot against a numeric USER, and a named
# one is rejected at admission with "container has runAsNonRoot and image
# has non-numeric user". Group 0 leaves the root-owned /app readable
# without a recursive chown, which is also what lets the image run under an
# arbitrary assigned uid.
RUN useradd --system --uid 10001 --gid 0 --no-create-home --shell /usr/sbin/nologin app
USER 10001

CMD ["/app/docker-entrypoint.sh"]
