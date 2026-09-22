#!/bin/sh
# Runtime entrypoint. Apply Alembic migrations (the ONLY place production Postgres DDL is
# created — AD-04/ADR-015a), then serve. DATABASE_URL comes from the environment.
set -e

# alembic.ini + migrations/ live at /app; run the upgrade from there, then serve from src.
cd /app
alembic upgrade head
cd /app/src
exec uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
