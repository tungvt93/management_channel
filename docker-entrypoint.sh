#!/usr/bin/env sh
set -e

echo "Waiting for PostgreSQL at db:5432..."
i=0
while ! python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('db', 5432)); s.close()" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "Timeout waiting for database."
    exit 1
  fi
  sleep 1
done

echo "Running migrations..."
alembic upgrade head

if [ "${RELOAD:-0}" = "1" ]; then
  echo "Starting server with auto-reload..."
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
