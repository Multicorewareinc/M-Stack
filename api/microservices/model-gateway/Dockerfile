# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY src/ ./src/

# Test stage: `docker build --target test -t model-gateway-test . && docker run --rm model-gateway-test`
FROM base AS test
RUN pip install pytest pytest-asyncio
# Tests are the canonical copy under api/tests/model-gateway/ (project.md); they are mounted
# at run, not copied in, so there is one source of truth. pytest.ini is generated to match the
# in-container layout (src/ and the mounted tests/ both under /app):
#   docker run --rm -v "$(pwd)/../../tests/model-gateway/tests:/app/tests" model-gateway-test
RUN printf '[pytest]\npythonpath = src\ntestpaths = tests\nasyncio_mode = auto\n' > pytest.ini
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image.
FROM base AS runtime
WORKDIR /app/src
EXPOSE 8080
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
