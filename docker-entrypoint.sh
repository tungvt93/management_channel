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

if [ ! -f /app/scripts/update_tiktok_followers.py ]; then
  echo "Generating missing scripts/update_tiktok_followers.py..."
  mkdir -p /app/scripts
  cat > /app/scripts/update_tiktok_followers.py <<'PY'
import asyncio
import os
import re

from sqlalchemy import select

from app.database import AsyncSessionLocal, init_db
from app.models import TikTokProfile
from playwright.async_api import async_playwright


def _parse_followers(html: str) -> int:
    m = re.findall(r'"followerCount"\s*:\s*(\d+)', html or "")
    if not m:
        return 0
    return max((int(x) for x in m), default=0)


async def _get_followers(url: str) -> int:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        c = await b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1365, "height": 768},
        )
        pg = await c.new_page()
        try:
            await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(2500)
            return _parse_followers(await pg.content())
        finally:
            await c.close()
            await b.close()


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
        updated = 0
        for p in profiles:
            url = (p.url or "").strip()
            if not url:
                continue
            try:
                new = int(await _get_followers(url))
            except Exception as exc:
                print(f"[WARN] Lỗi lấy follower: {url} -> {exc}")
                continue
            old = int(p.followers_count or 0)
            if new != old:
                p.followers_count = new
                updated += 1
            print(url, "old=", old, "new=", new)
        await db.commit()
        print(f"Done. Profiles={len(profiles)}, updated={updated}.")


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
