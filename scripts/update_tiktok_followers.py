"""
Cronjob: quét list TikTok profiles và cập nhật followers_count + view 5 video mới nhất.

Chạy thủ công:
  python scripts/update_tiktok_followers.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models import TikTokCookieSetting, TikTokProfile, TikTokSyncRun  # noqa: E402
from app.scraper import get_tiktok_followers_count, get_tiktok_latest_videos_with_views  # noqa: E402


def _as_int(v: Optional[int]) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


async def main() -> None:
    await init_db()

    limit = int(os.getenv("TIKTOK_FOLLOWERS_SCAN_LIMIT", "0") or "0")  # 0 = không giới hạn

    async with AsyncSessionLocal() as db:
        run = TikTokSyncRun(kind="cron", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        cookie_res = await db.execute(select(TikTokCookieSetting).order_by(TikTokCookieSetting.updated_at.desc()))
        cookie_setting = cookie_res.scalars().first()
        cookie_json = cookie_setting.cookie_json if cookie_setting else None

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

            try:
                followers = await get_tiktok_followers_count(url, cookie_json_text=cookie_json)
            except Exception as exc:
                print(f"[WARN] Lỗi lấy follower: {url} -> {exc}")
            else:
                followers = _as_int(followers)
                old = _as_int(p.followers_count)
                if followers != old:
                    p.followers_count = followers
                    followers_updated += 1

            try:
                latest_videos = await get_tiktok_latest_videos_with_views(url, limit=5, cookie_json_text=cookie_json)
            except Exception as exc:
                print(f"[WARN] Lỗi lấy view 5 video mới nhất: {url} -> {exc}")
            else:
                p.latest_videos_json = json.dumps(latest_videos, ensure_ascii=False)
                p.last_synced_at = datetime.now(timezone.utc)
                videos_updated += 1

        # Mark run done
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        print(
            "Done. "
            f"Profiles={len(profiles)}, "
            f"followers_updated={followers_updated}, "
            f"video_snapshots_updated={videos_updated}."
        )


if __name__ == "__main__":
    asyncio.run(main())

