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

echo "Running YouTube PubSubHubbub renew on startup..."
python /app/scripts/run_pubsub_renew.py || echo "Warning: YouTube PubSubHubbub renew failed but continuing..."

if [ ! -f /app/scripts/update_tiktok_followers.py ]; then
  echo "Generating missing scripts/update_tiktok_followers.py..."
  mkdir -p /app/scripts
  cat > /app/scripts/update_tiktok_followers.py <<'PY'
import asyncio
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal, init_db
from app.models import TikTokProfile
from app.scraper import get_tiktok_followers_count, get_tiktok_latest_videos_with_views


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


async def main() -> None:
    await init_db()
    limit = int(os.getenv("TIKTOK_FOLLOWERS_SCAN_LIMIT", "0") or "0")
    async with AsyncSessionLocal() as db:
        stmt = select(TikTokProfile).order_by(TikTokProfile.created_at.desc())
        if limit > 0:
            stmt = stmt.limit(limit)
        res = await db.execute(stmt)
        profiles = res.scalars().all()
        if not profiles:
            print("Không có TikTok profile nào để quét.")
            return
        followers_updated = 0
        videos_updated = 0
        for p in profiles:
            url = (p.url or "").strip()
            if not url:
                continue
            latest_videos = []
            try:
                new = _as_int(await get_tiktok_followers_count(url))
            except Exception as exc:
                print(f"[WARN] Lỗi lấy follower: {url} -> {exc}")
            else:
                old = _as_int(p.followers_count)
                if new != old:
                    p.followers_count = new
                    followers_updated += 1
            try:
                latest_videos = await get_tiktok_latest_videos_with_views(url, limit=5)
            except Exception as exc:
                print(f"[WARN] Lỗi lấy view 5 video mới nhất: {url} -> {exc}")
            else:
                p.latest_videos_json = json.dumps(latest_videos, ensure_ascii=False)
                p.last_synced_at = datetime.now(timezone.utc)
                videos_updated += 1
            print(url, "followers=", _as_int(p.followers_count), "videos=", len(latest_videos))
        await db.commit()
        print(f"Done. Profiles={len(profiles)}, followers_updated={followers_updated}, video_snapshots_updated={videos_updated}.")


if __name__ == "__main__":
    asyncio.run(main())
PY
  chmod 0644 /app/scripts/update_tiktok_followers.py
fi

echo "Setting up cronjob (07:00 daily) to update TikTok followers..."
# Export current container env vars for cron jobs (cron runs with minimal env)
printenv | sed 's/^\(.*\)$/export \1/g' > /etc/profile.d/container_env.sh
chmod 0644 /etc/profile.d/container_env.sh

# Create cron file
cat > /etc/cron.d/tiktok_followers <<'EOF'
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Run everyday at 07:00 (container timezone). Logs to /var/log/cron.log
0 7 * * * root . /etc/profile.d/container_env.sh && cd /app && python scripts/update_tiktok_followers.py >> /var/log/cron.log 2>&1
EOF
chmod 0644 /etc/cron.d/tiktok_followers

touch /var/log/cron.log

# Start cron daemon (background)
if command -v cron >/dev/null 2>&1; then
  CRON_BIN="$(command -v cron)"
elif [ -x /usr/sbin/cron ]; then
  CRON_BIN="/usr/sbin/cron"
elif command -v crond >/dev/null 2>&1; then
  CRON_BIN="$(command -v crond)"
else
  echo "ERROR: cron is not installed in the image. Rebuild the web image."
  exit 1
fi

echo "Starting cron daemon: ${CRON_BIN}"
"${CRON_BIN}"

if [ "${RELOAD:-0}" = "1" ]; then
  echo "Starting server with auto-reload..."
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
