# syntax=docker/dockerfile:1
#
# Build from the REPO ROOT, not from this folder -- since ADR-033 this service reaches the
# event backbone through the sibling `multistack` SDK package, which has to be inside the
# build context (same reason agentic/backend/Dockerfile does it):
#   docker build -f api/microservices/rate-limiter-rpm/Dockerfile -t rate-limiter-rpm .
#   docker build -f api/microservices/rate-limiter-rpm/Dockerfile --target test -t rate-limiter-rpm-test .
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# The multistack SDK (one hard dep, pydantic -- see its own pyproject.toml), installed
# editable so `from multistack.queue import ...` resolves. The `[nats]` extra brings the
# backend client the queue driver imports: it is the SDK's dependency, not this service's,
# which is what keeps a backend swap out of every microservice.
COPY pyproject.toml README.md ./
COPY multistack/ ./multistack/
RUN pip install --upgrade pip && pip install -e ".[nats]"

# This service's own dependencies.
COPY api/microservices/rate-limiter-rpm/requirements.txt .
RUN pip install -r requirements.txt
COPY api/microservices/rate-limiter-rpm/src/ ./src/

# Test stage: `docker build -f api/microservices/rate-limiter-rpm/Dockerfile --target test -t rate-limiter-rpm-test .`
# httpx is a test-only tool (Starlette TestClient imports it), same category as pytest.
FROM base AS test
RUN pip install pytest pytest-asyncio httpx
# Tests are the canonical copy under api/tests/rate-limiter-rpm/ (project.md); they are mounted
# at run, not copied in, so there is one source of truth. pytest.ini is generated to match the
# in-container layout (src/ and the mounted tests/ both under /app):
#   docker run --rm -v "$(pwd)/api/tests/rate-limiter-rpm/tests:/app/tests" rate-limiter-rpm-test
RUN printf '[pytest]\npythonpath = src\ntestpaths = tests\nasyncio_mode = auto\n' > pytest.ini
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image.
FROM base AS runtime
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

CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
