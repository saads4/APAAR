#!/bin/sh
set -e
echo "Applying migrations ..."
alembic upgrade head
if [ "${SEED_DEMO_DATA:-true}" = "true" ]; then
  echo "Seeding synthetic demo data (skipped if already present) ..."
  python -m app.seed
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
