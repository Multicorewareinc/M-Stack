# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# tiktoken does not ship its encodings -- get_encoding() downloads the
# .tiktoken file from openaipublic.blob.core.windows.net on first use and
# caches it. A cluster pod with no egress to that host (this one has none)
# gets a ConnectionError from the very first /tokenize call: /health never
# calls get_encoding(), so the probe stays green while every real request
# 500s. Baked in at build time instead, into a directory both the `test`
# and `runtime` stages below inherit, so the image needs no network access
# ever again to do its one job.
#
# _ENCODERS in src/service.py has one entry (cl100k_base) today; a second
# encoding needs a second line added here, or it will fail exactly the
# same way the first one did.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY src/ ./src/

# Test stage: `docker build --target test -t tokenizer-test . && docker run --rm tokenizer-test`
# httpx is a test-only tool (Starlette TestClient imports it), same category as pytest.
FROM base AS test
RUN pip install pytest pytest-asyncio httpx
# Tests are the canonical copy under api/tests/tokenizer/ (project.md); they are mounted at
# run, not copied in, so there is one source of truth. pytest.ini is generated to match the
# in-container layout (src/ and the mounted tests/ both under /app):
#   docker run --rm -v "$(pwd)/../../tests/tokenizer/tests:/app/tests" tokenizer-test
RUN printf '[pytest]\npythonpath = src\ntestpaths = tests\nasyncio_mode = auto\n' > pytest.ini
CMD ["pytest", "-q"]

# Runtime stage (default target): the server image.
FROM base AS runtime
WORKDIR /app/src
EXPOSE 8000
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
