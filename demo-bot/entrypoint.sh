#!/bin/sh
set -e

# Ждём PostgreSQL (healthcheck в compose уже это делает, тут — страховка для `docker compose run`)
python - <<'PY'
import os, time
import psycopg
for attempt in range(30):
    try:
        psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "db"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "servicebot"),
            user=os.environ.get("POSTGRES_USER", "servicebot"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            connect_timeout=2,
        ).close()
        break
    except Exception:
        time.sleep(1)
PY

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput >/dev/null
elif [ "${WAIT_FOR_MIGRATIONS:-0}" = "1" ]; then
    # Бот стартует параллельно с вебом — ждём, пока веб применит миграции
    until python manage.py migrate --check >/dev/null 2>&1; do
        echo "Жду применения миграций..."
        sleep 2
    done
fi

exec "$@"
