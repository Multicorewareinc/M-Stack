#!/bin/sh
# Runtime entrypoint. Apply Alembic migrations (the ONLY place production Postgres DDL is
# created), then serve. DATABASE_URL comes from the environment.
set -e

cd /app
alembic upgrade head
cd /app/src
exec uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
