"""
Cronjob: quét list TikTok profiles và cập nhật followers_count.

Chạy thủ công:
  python scripts/update_tiktok_followers.py
"""

import asyncio
import os
import sys
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models import TikTokProfile  # noqa: E402
from app.scraper import get_tiktok_followers_count  # noqa: E402


def _as_int(v: Optional[int]) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


async def main() -> None:
    await init_db()

    limit = int(os.getenv("TIKTOK_FOLLOWERS_SCAN_LIMIT", "0") or "0")  # 0 = không giới hạn

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
                followers = await get_tiktok_followers_count(url)
            except Exception as exc:
                print(f"[WARN] Lỗi lấy follower: {url} -> {exc}")
                continue

            followers = _as_int(followers)
            old = _as_int(p.followers_count)
            if followers != old:
                p.followers_count = followers
                updated += 1

        await db.commit()
        print(f"Done. Profiles={len(profiles)}, updated={updated}.")


if __name__ == "__main__":
    asyncio.run(main())

