import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import List, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from starlette.middleware.sessions import SessionMiddleware
import csv
import io

from .auth import verify_password
from .database import AsyncSessionLocal, get_db, init_db
from .models import (
    Channel,
    ChannelManager,
    Platform,
    ScrapingStatus,
    User,
    VideoLink,
    VideoStatus,
    TikTokProfile,
    TikTokCookieSetting,
    TikTokSyncRun,
    TikTokSyncScheduleSetting,
    TIKTOK_PROFILE_UPLOAD_STATUSES,
    YoutubeChannel,
)
from .scraper import (
    get_tiktok_followers_count,
    get_tiktok_latest_videos_with_views,
    get_tiktok_videos,
    get_youtube_videos,
    get_youtube_channel_id,
    subscribe_youtube_pubsub,
    is_youtube_short,
)

logger = logging.getLogger(__name__)
_HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_TIKTOK_SYNC_JOB_ID = "tiktok_profiles_sync_cron_job"
_TIKTOK_CHANNELS_DAILY_JOB_ID = "tiktok_channels_daily_midnight_job"
_YOUTUBE_PUBSUB_RENEW_JOB_ID = "youtube_pubsub_weekly_renew_job"
_sync_scheduler = AsyncIOScheduler(timezone=_HCM_TZ)

# Đường dẫn gốc dự án (uvicorn có thể chạy với cwd khác — không dùng relative "templates"/"static").
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

app = FastAPI(title="Channel Content Manager")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv(
        "SECRET_KEY",
        "dev-secret-change-in-production-use-long-random-string",
    ),
    same_site="lax",
)

# Setup templates and static files
app.mount(
    "/static",
    StaticFiles(directory=str(_PROJECT_ROOT / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))
# Tránh Jinja cache template khiến sửa HTML không hiện ngay.
# (Không ảnh hưởng lớn vì app này chủ yếu render HTML đơn giản.)
try:
    templates.env.auto_reload = True
    templates.env.cache = {}
except Exception:
    pass


async def require_login_api(request: Request) -> None:
    if request.session.get("user_id") is None:
        raise HTTPException(status_code=401, detail="Not authenticated")


# Pydantic Schemas
class ChannelCreate(BaseModel):
    url: str


class ChannelResponse(BaseModel):
    id: int
    url: str
    platform: Platform
    name: Optional[str]
    manager: Optional[ChannelManager]
    scraping_status: ScrapingStatus
    last_scraped_at: Optional[datetime]
    scraping_error: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class VideoUpdate(BaseModel):
    status: VideoStatus

class VideoResponse(BaseModel):
    id: int
    url: str
    status: VideoStatus
    upload_date: Optional[datetime]
    channel_id: int

    class Config:
        from_attributes = True

@app.on_event("startup")
async def startup():
    await init_db()
    if not _sync_scheduler.running:
        _sync_scheduler.start()
    _register_tiktok_channels_daily_job()
    _register_youtube_pubsub_renew_job()
    await _reload_tiktok_sync_schedule_from_db()


@app.on_event("shutdown")
async def shutdown():
    if _sync_scheduler.running:
        _sync_scheduler.shutdown(wait=False)


def _validate_schedule_time(hour: int, minute: int) -> None:
    if hour < 0 or hour > 23:
        raise ValueError("Giờ chạy phải nằm trong khoảng 0-23.")
    if minute < 0 or minute > 59:
        raise ValueError("Phút chạy phải nằm trong khoảng 0-59.")


async def _apply_tiktok_sync_schedule(enabled: bool, hour: int, minute: int) -> None:
    """
    Luôn xóa job cũ trước khi add lại để tránh duplicate job khi đổi lịch.
    """
    _validate_schedule_time(hour, minute)
    old_job = _sync_scheduler.get_job(_TIKTOK_SYNC_JOB_ID)
    if old_job is not None:
        _sync_scheduler.remove_job(_TIKTOK_SYNC_JOB_ID)
    if not enabled:
        logger.info("TikTok cron schedule disabled")
        return

    _sync_scheduler.add_job(
        _run_tiktok_cron_sync_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=_HCM_TZ),
        id=_TIKTOK_SYNC_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("TikTok cron schedule set to %02d:%02d (HCM)", hour, minute)


async def _reload_tiktok_sync_schedule_from_db() -> None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(TikTokSyncScheduleSetting).order_by(TikTokSyncScheduleSetting.updated_at.desc())
        )
        setting = res.scalars().first()
    if setting is None:
        await _apply_tiktok_sync_schedule(enabled=False, hour=7, minute=0)
        return
    try:
        await _apply_tiktok_sync_schedule(
            enabled=bool(setting.enabled),
            hour=int(setting.hour or 0),
            minute=int(setting.minute or 0),
        )
    except ValueError:
        logger.exception("TikTok sync schedule trong DB không hợp lệ; tắt schedule để an toàn.")
        await _apply_tiktok_sync_schedule(enabled=False, hour=7, minute=0)


async def _run_tiktok_cron_sync_job() -> None:
    async with AsyncSessionLocal() as db:
        run = TikTokSyncRun(kind="cron", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = int(run.id)
    await refresh_all_tiktok_profiles_stats_task_with_run(run_id)

# Background Task for Scraping
# Own DB session: the request-scoped session from Depends(get_db) is closed after the response; do not pass it into BackgroundTasks.
async def scrape_channel_task(channel_id: int, channel_url: str, platform: Platform):
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Channel).where(Channel.id == channel_id).values(
                scraping_status=ScrapingStatus.IN_PROGRESS,
                scraping_error=None
            )
        )
        await db.commit()

        try:
            if platform == Platform.YOUTUBE:
                videos = await get_youtube_videos(channel_url)
            else:
                videos = await get_tiktok_videos(channel_url)

            for video_data in videos:
                url = video_data["url"]
                stmt = select(VideoLink).where(VideoLink.url == url)
                result = await db.execute(stmt)
                if result.scalar_one_or_none():
                    continue
                try:
                    async with db.begin_nested():
                        new_video = VideoLink(
                            channel_id=channel_id,
                            url=url,
                            upload_date=video_data["upload_date"],
                            status=VideoStatus.AVAILABLE,
                        )
                        db.add(new_video)
                except IntegrityError:
                    logger.warning(
                        "Bỏ qua video trùng URL (race hoặc ràng buộc DB): %s",
                        url,
                    )

            await db.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    scraping_status=ScrapingStatus.SUCCESS,
                    last_scraped_at=datetime.now()
                )
            )
            await db.commit()
        except Exception as e:
            logger.exception("Lỗi quét kênh %s", channel_url)
            print(f"Error scraping {channel_url}: {e}")
            await db.rollback()
            await db.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    scraping_status=ScrapingStatus.FAILED,
                    scraping_error=str(e)
                )
            )
            await db.commit()


async def _sync_single_tiktok_channel_videos(
    db: AsyncSession,
    channel_id: int,
    channel_url: str,
) -> None:
    await db.execute(
        update(Channel).where(Channel.id == channel_id).values(
            scraping_status=ScrapingStatus.IN_PROGRESS,
            scraping_error=None,
        )
    )
    await db.commit()

    try:
        cookie_json = await _get_tiktok_cookie_json()
        videos = await get_tiktok_videos(channel_url, cookie_json_text=cookie_json)

        for idx, video_data in enumerate(videos):
            if not isinstance(video_data, dict):
                logger.warning(
                    "Skip invalid tiktok video item channel_id=%s index=%s: not a dict",
                    channel_id,
                    idx,
                )
                continue
            raw_url = video_data.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            if not url:
                logger.warning(
                    "Skip invalid tiktok video item channel_id=%s index=%s: missing url",
                    channel_id,
                    idx,
                )
                continue
            exists = await db.execute(select(VideoLink).where(VideoLink.url == url))
            if exists.scalar_one_or_none():
                continue
            try:
                async with db.begin_nested():
                    db.add(
                        VideoLink(
                            channel_id=channel_id,
                            url=url,
                            upload_date=video_data.get("upload_date"),
                            status=VideoStatus.AVAILABLE,
                        )
                    )
            except IntegrityError:
                logger.warning("Skip duplicate url=%s", url)

        await db.execute(
            update(Channel).where(Channel.id == channel_id).values(
                scraping_status=ScrapingStatus.SUCCESS,
                last_scraped_at=datetime.now(timezone.utc),
                scraping_error=None,
            )
        )
        await db.commit()
    except Exception as exc:
        logger.exception("Daily sync failed for channel=%s", channel_url)
        await db.rollback()
        await db.execute(
            update(Channel).where(Channel.id == channel_id).values(
                scraping_status=ScrapingStatus.FAILED,
                scraping_error=str(exc),
            )
        )
        await db.commit()


async def refresh_all_tiktok_channels_videos_daily_task() -> None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Channel.id, Channel.url).where(Channel.platform == Platform.TIKTOK)
        )
        rows = res.all()

    for channel_id, channel_url in rows:
        async with AsyncSessionLocal() as db:
            try:
                await _sync_single_tiktok_channel_videos(
                    db=db,
                    channel_id=int(channel_id),
                    channel_url=str(channel_url or ""),
                )
            except Exception:
                logger.exception(
                    "Daily tiktok channel sync failed channel_id=%s",
                    channel_id,
                )


async def _run_tiktok_channels_daily_sync_job() -> None:
    await refresh_all_tiktok_channels_videos_daily_task()


def _register_tiktok_channels_daily_job() -> None:
    old_job = _sync_scheduler.get_job(_TIKTOK_CHANNELS_DAILY_JOB_ID)
    if old_job is not None:
        _sync_scheduler.remove_job(_TIKTOK_CHANNELS_DAILY_JOB_ID)

    _sync_scheduler.add_job(
        _run_tiktok_channels_daily_sync_job,
        trigger=CronTrigger(hour=0, minute=0, timezone=_HCM_TZ),
        id=_TIKTOK_CHANNELS_DAILY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


async def _run_youtube_pubsub_renew_job() -> None:
    """Renew tất cả YouTube PubSubHubbub subscriptions (chạy 0h thứ 2 hàng tuần)."""
    callback_base = os.getenv("CALLBACK_BASE_URL", "").strip()
    if not callback_base:
        logger.warning("CALLBACK_BASE_URL not set; skipping YouTube PubSubHubbub weekly renew")
        return

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(YoutubeChannel).where(YoutubeChannel.channel_id.isnot(None))
        )
        channels = res.scalars().all()

    if not channels:
        logger.info("YouTube PubSubHubbub renew: không có channel nào để renew")
        return

    logger.info("YouTube PubSubHubbub renew: bắt đầu renew %d channel", len(channels))
    success = 0
    for ch in channels:
        try:
            ok = await subscribe_youtube_pubsub(ch.channel_id, callback_base, mode="subscribe")
            if ok:
                success += 1
        except Exception:
            logger.exception("YouTube PubSubHubbub renew failed for channel_id=%s", ch.channel_id)

    logger.info("YouTube PubSubHubbub renew: %d/%d thành công", success, len(channels))


def _register_youtube_pubsub_renew_job() -> None:
    old_job = _sync_scheduler.get_job(_YOUTUBE_PUBSUB_RENEW_JOB_ID)
    if old_job is not None:
        _sync_scheduler.remove_job(_YOUTUBE_PUBSUB_RENEW_JOB_ID)

    _sync_scheduler.add_job(
        _run_youtube_pubsub_renew_job,
        trigger=CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=_HCM_TZ),
        id=_YOUTUBE_PUBSUB_RENEW_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("YouTube PubSubHubbub renew job registered: 0h thứ 2 hàng tuần (HCM)")


async def refresh_tiktok_profile_followers_task(profile_id: int, profile_url: str) -> None:
    """Cập nhật followers sau khi tạo profile (retry khi lần đầu Playwright/parse trả 0)."""
    url = (profile_url or "").strip()
    if not url:
        return
    cookie_json = await _get_tiktok_cookie_json()
    try:
        n = int(await get_tiktok_followers_count(url, cookie_json_text=cookie_json))
    except Exception as exc:
        logger.warning(
            "Background: không lấy được followers profile_id=%s url=%s: %s",
            profile_id,
            url,
            exc,
        )
        return
    if n <= 0:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TikTokProfile)
            .where(TikTokProfile.id == profile_id)
            .values(followers_count=n)
        )
        await db.commit()


async def _get_tiktok_cookie_json() -> Optional[str]:
    """Lấy cookie TikTok từ DB (ưu tiên) để dùng cho scraper/cron."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(TikTokCookieSetting).order_by(TikTokCookieSetting.updated_at.desc()))
        setting = res.scalars().first()
        return setting.cookie_json if setting else None


def _compute_cookie_expires_at(cookie_json_text: str) -> Optional[datetime]:
    """
    Lấy expires_at ước tính từ cookie.json export (max expirationDate).
    """
    import json as _json

    try:
        data = _json.loads(cookie_json_text or "")
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    max_ts: Optional[float] = None
    for c in data:
        if not isinstance(c, dict):
            continue
        exp = c.get("expirationDate")
        if isinstance(exp, (int, float)) and exp > 0:
            max_ts = exp if max_ts is None else max(max_ts, float(exp))
    if not max_ts:
        return None
    # một số export là seconds epoch
    try:
        return datetime.fromtimestamp(int(max_ts), tz=timezone.utc)
    except Exception:
        return None

async def refresh_tiktok_profile_stats_task(profile_id: int, profile_url: str) -> None:
    """Cập nhật followers + snapshot 5 video mới nhất (views) cho 1 profile."""
    url = (profile_url or "").strip()
    if not url:
        return

    cookie_json = await _get_tiktok_cookie_json()
    try:
        followers = int(await get_tiktok_followers_count(url, cookie_json_text=cookie_json))
    except Exception as exc:
        logger.warning("Không lấy được followers url=%s: %s", url, exc)
        followers = 0

    try:
        latest = await get_tiktok_latest_videos_with_views(url, limit=5, cookie_json_text=cookie_json)
    except Exception as exc:
        logger.warning("Không lấy được latest videos url=%s: %s", url, exc)
        latest = []

    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    # Lưu thời điểm upload video mới nhất (nếu có timestamp hợp lệ)
    last_published_at = None
    try:
        if latest and isinstance(latest[0], dict):
            ts0 = latest[0].get("timestamp")
            if isinstance(ts0, (int, float)) and ts0 > 0:
                last_published_at = _dt.fromtimestamp(int(ts0), tz=_tz.utc)
    except Exception:
        last_published_at = None

    # Fallback: nếu không extract được views/timestamp (TikTok chặn), dùng extract_flat để lấy timestamp/upload_date.
    if last_published_at is None:
        try:
            flat_videos = await get_tiktok_videos(url, cookie_json_text=cookie_json)
        except Exception as exc:
            logger.warning("Không lấy được danh sách video (fallback) url=%s: %s", url, exc)
            flat_videos = []
        try:
            best_dt = None
            for v in flat_videos or []:
                if not isinstance(v, dict):
                    continue
                ud = v.get("upload_date")
                if not isinstance(ud, _dt):
                    continue
                # upload_date trong scraper có thể là naive; coi là UTC để tránh crash.
                if ud.tzinfo is None:
                    ud = ud.replace(tzinfo=_tz.utc)
                best_dt = ud if best_dt is None else max(best_dt, ud)
            last_published_at = best_dt
        except Exception:
            last_published_at = None

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TikTokProfile)
            .where(TikTokProfile.id == profile_id)
            .values(
                followers_count=followers,
                latest_videos_json=_json.dumps(latest, ensure_ascii=False),
                last_video_published_at=last_published_at,
                last_synced_at=_dt.now(_tz.utc),
            )
        )
        await db.commit()


async def refresh_all_tiktok_profiles_stats_task() -> None:
    """Cập nhật followers + views (5 video mới nhất) cho tất cả TikTok profiles."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(TikTokProfile.id, TikTokProfile.url))
        rows = res.all()

    # Chạy tuần tự để tránh bắn quá nhiều request Playwright/yt-dlp cùng lúc.
    for pid, url in rows:
        try:
            await refresh_tiktok_profile_stats_task(int(pid), str(url or ""))
        except Exception:
            logger.exception("Sync tiktok profile failed id=%s", pid)


async def refresh_all_tiktok_profiles_stats_task_with_run(run_id: int) -> None:
    """Chạy sync tất cả và ghi nhận trạng thái vào `tiktok_sync_runs`."""
    started = datetime.now(timezone.utc)
    status = "success"
    message = None
    try:
        await refresh_all_tiktok_profiles_stats_task()
    except Exception as exc:
        status = "failed"
        message = str(exc)
        logger.exception("Sync run failed run_id=%s", run_id)
    finished = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TikTokSyncRun)
            .where(TikTokSyncRun.id == run_id)
            .values(
                status=status,
                started_at=started,
                finished_at=finished,
                message=message,
            )
        )
        await db.commit()


# 1. API: Get list of available video links
@app.get(
    "/api/videos",
    response_model=Optional[VideoResponse],    
)
async def get_available_videos(channel_link: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    stmt = select(VideoLink).where(VideoLink.status == VideoStatus.AVAILABLE)
    if channel_link:
        stmt = stmt.join(Channel).where(Channel.url == channel_link)
    
    # Lấy 1 bản ghi và lock để tránh race condition
    stmt = stmt.limit(1).with_for_update(skip_locked=True)
    
    result = await db.execute(stmt)
    video = result.scalar_one_or_none()
    
    if video:
        video.status = VideoStatus.HOLDED
        await db.commit()
        await db.refresh(video)
    
    return video

# 2. API: Update video status
@app.patch(
    "/api/videos/{video_id}",
    response_model=VideoResponse,    
)
async def update_video_status(video_id: int, status_update: VideoUpdate, db: AsyncSession = Depends(get_db)):
    stmt = update(VideoLink).where(VideoLink.id == video_id).values(status=status_update.status).returning(VideoLink)
    result = await db.execute(stmt)
    updated_video = result.scalar_one_or_none()
    
    if not updated_video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    await db.commit()
    return updated_video


@app.post(
    "/api/videos/import-done",
    dependencies=[Depends(require_login_api)],
)
async def import_videos_mark_done(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Import CSV gồm 1 cột duy nhất là link video.
    Với mỗi link trùng trong DB thì update status -> DONE.
    """
    raw = await file.read()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("utf-8", errors="replace")

    reader = csv.reader(io.StringIO(decoded))

    urls: set[str] = set()
    for row in reader:
        if not row:
            continue
        url = (row[0] or "").strip()
        if not url:
            continue
        low = url.lower()
        if low in {"url", "link", "video_url", "video link"}:
            continue
        urls.add(url)

    if not urls:
        raise HTTPException(status_code=400, detail="CSV không có link hợp lệ.")

    existing_res = await db.execute(select(VideoLink.url).where(VideoLink.url.in_(urls)))
    existing_urls = set(existing_res.scalars().all())
    not_found = sorted(urls - existing_urls)

    if existing_urls:
        upd_stmt = (
            update(VideoLink)
            .where(VideoLink.url.in_(existing_urls))
            .values(status=VideoStatus.DONE)
        )
        result = await db.execute(upd_stmt)
        updated_count = int(result.rowcount or 0)
        await db.commit()
    else:
        updated_count = 0

    return {
        "total_in_csv": len(urls),
        "matched_in_db": len(existing_urls),
        "updated_to_done": updated_count,
        "not_found_count": len(not_found),
        "not_found_sample": not_found[:20],
        "message": f"Đã cập nhật DONE: {updated_count}/{len(urls)} link (không tìm thấy: {len(not_found)}).",
    }

# 3. API: Add channel and trigger scrape
@app.post("/api/channels")
async def add_channel(channel_in: ChannelCreate, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    # Simple platform detection
    url = channel_in.url.lower()
    if "youtube.com" in url or "youtu.be" in url:
        platform = Platform.YOUTUBE
    elif "tiktok.com" in url:
        platform = Platform.TIKTOK
    else:
        raise HTTPException(status_code=400, detail="Unsupported platform. Only YouTube and TikTok are allowed.")

    # Check existence
    stmt = select(Channel).where(Channel.url == channel_in.url)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    
    if not channel:
        channel = Channel(url=channel_in.url, platform=platform)
        db.add(channel)
        await db.commit()
        await db.refresh(channel)
        
    # Trigger background scrape
    background_tasks.add_task(scrape_channel_task, channel.id, channel.url, platform)
    
    return {"message": "Channel added and scraping started", "channel_id": channel.id}


@app.get(
    "/api/channels",
    response_model=List[ChannelResponse],
)
async def list_channels(db: AsyncSession = Depends(get_db)):
    """Trả về danh sách tất cả kênh (mới nhất trước)."""
    result = await db.execute(select(Channel).order_by(Channel.created_at.desc()))
    return result.scalars().all()


@app.post(
    "/api/channels/{channel_id}/manager",
    dependencies=[Depends(require_login_api)],
)
async def update_channel_manager(
    channel_id: int,
    manager: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    m = (manager or "").strip().lower()
    if m == "":
        new_value = None
    elif m in {ChannelManager.TUNG.value, ChannelManager.LONG.value}:
        new_value = ChannelManager(m)
    else:
        raise HTTPException(status_code=400, detail="manager không hợp lệ")

    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel.manager = new_value
    await db.commit()
    return {"ok": True, "channel_id": channel_id, "manager": new_value.value if new_value else None}


# 4. API: Get channel scraping status
@app.get(
    "/api/channels/{channel_id}/status",
    dependencies=[Depends(require_login_api)],
)
async def get_channel_status(channel_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {
        "channel_id": channel.id,
        "scraping_status": channel.scraping_status,
        "last_scraped_at": channel.last_scraped_at,
        "scraping_error": channel.scraping_error,
    }


def _scraping_status_key(status) -> str:
    if isinstance(status, ScrapingStatus):
        return status.value
    return status or ScrapingStatus.IDLE.value


# 5. API: Re-run scrape for an existing channel
@app.post(
    "/api/channels/{channel_id}/scrape",
    dependencies=[Depends(require_login_api)],
)
async def rescrape_channel(
    channel_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if _scraping_status_key(channel.scraping_status) == ScrapingStatus.IN_PROGRESS.value:
        raise HTTPException(
            status_code=409,
            detail="Quét đang chạy cho kênh này, vui lòng đợi.",
        )
    background_tasks.add_task(scrape_channel_task, channel.id, channel.url, channel.platform)
    return {"message": "Rescrape started", "channel_id": channel.id}


@app.post(
    "/api/channels/{channel_id}/delete",
    dependencies=[Depends(require_login_api)],
)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    # Xoá video_links trước để chắc chắn không vướng FK (bulk delete bỏ qua ORM cascade).
    await db.execute(delete(VideoLink).where(VideoLink.channel_id == channel_id))
    result = await db.execute(delete(Channel).where(Channel.id == channel_id))
    deleted = int(result.rowcount or 0)
    if deleted == 0:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Channel not found")
    await db.commit()
    return {"message": "Channel deleted", "channel_id": channel_id}


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": request.query_params.get("error"),
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    db: AsyncSession = Depends(get_db),
):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=1", status_code=302)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# Dashboard route
@app.get("/")
async def dashboard(
    request: Request,
    channel_id: Optional[int] = None,
    channel_search: Optional[str] = None,
    video_search: Optional[str] = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    page_size = 20
    offset = (page - 1) * page_size

    # Fetch channels (optionally filter by URL substring)
    channels_stmt = select(Channel).order_by(Channel.created_at.desc())
    channel_search_term = (channel_search or "").strip()
    if channel_search_term:
        channels_stmt = channels_stmt.where(Channel.url.ilike(f"%{channel_search_term}%"))

    channels_result = await db.execute(channels_stmt)
    channels = channels_result.scalars().all()

    # Map channel_id -> total videos
    channel_video_counts: dict[int, int] = {}
    channel_ids = [c.id for c in channels]
    if channel_ids:
        counts_stmt = (
            select(VideoLink.channel_id, func.count(VideoLink.id))
            .where(VideoLink.channel_id.in_(channel_ids))
            .group_by(VideoLink.channel_id)
        )
        counts_result = await db.execute(counts_stmt)
        channel_video_counts = {cid: int(cnt) for cid, cnt in counts_result.all()}
    
    # Build video query with optional filter
    video_stmt = select(VideoLink).order_by(VideoLink.upload_date.desc(), VideoLink.created_at.desc())
    count_stmt = select(func.count()).select_from(VideoLink)

    video_search_term = (video_search or "").strip()
    if video_search_term:
        video_stmt = video_stmt.where(VideoLink.url.ilike(f"%{video_search_term}%"))
        count_stmt = count_stmt.where(VideoLink.url.ilike(f"%{video_search_term}%"))

    if channel_id:
        video_stmt = video_stmt.where(VideoLink.channel_id == channel_id)
        count_stmt = count_stmt.where(VideoLink.channel_id == channel_id)

    # Execute pagination
    video_stmt = video_stmt.limit(page_size).offset(offset)
    
    videos_result = await db.execute(video_stmt)
    videos = videos_result.scalars().all()

    # Get total count for pagination
    total_count_result = await db.execute(count_stmt)
    total_count = total_count_result.scalar()
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "channels": channels,
            "channel_video_counts": channel_video_counts,
            "videos": videos,
            "selected_channel": channel_id,
            "channel_search": channel_search_term,
            "video_search": video_search_term,
            "current_page": page,
            "total_pages": total_pages,
            "total_count": total_count,
            "Platform": Platform,
            "VideoStatus": VideoStatus,
            "active_menu": "dashboard"
        }
    )


# TikTok Profile Management Routes
@app.get("/tiktok-profiles")
async def tiktok_profiles_page(
    request: Request,
    sort: Optional[str] = None,
    manager: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    
    page = int(page or 1)
    if page < 1:
        page = 1
    page_size = int(page_size or 50)
    if page_size < 5:
        page_size = 5
    if page_size > 200:
        page_size = 200

    sort_key = (sort or "").strip().lower()
    if sort_key == "followers_desc":
        base_stmt = select(TikTokProfile).order_by(
            TikTokProfile.followers_count.desc(),
            TikTokProfile.created_at.desc(),
        )
    else:
        sort_key = "created_desc"
        base_stmt = select(TikTokProfile).order_by(TikTokProfile.created_at.desc())

    manager_filter = (manager or "").strip().lower()
    if manager_filter not in {"", ChannelManager.TUNG.value, ChannelManager.LONG.value}:
        manager_filter = ""

    status_filter = (status or "").strip().lower()
    if status_filter and status_filter not in TIKTOK_PROFILE_UPLOAD_STATUSES:
        status_filter = ""

    if manager_filter:
        base_stmt = base_stmt.where(TikTokProfile.manager == ChannelManager(manager_filter))
    if status_filter:
        base_stmt = base_stmt.where(TikTokProfile.upload_status == status_filter)

    total_count_stmt = select(func.count()).select_from(TikTokProfile)
    if manager_filter:
        total_count_stmt = total_count_stmt.where(TikTokProfile.manager == ChannelManager(manager_filter))
    if status_filter:
        total_count_stmt = total_count_stmt.where(TikTokProfile.upload_status == status_filter)

    total_count_res = await db.execute(total_count_stmt)
    total_count = int(total_count_res.scalar() or 0)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size

    stmt = base_stmt.limit(page_size).offset(offset)
    result = await db.execute(stmt)
    profiles = result.scalars().all()

    # Cookie status
    cookie_res = await db.execute(
        select(TikTokCookieSetting).order_by(TikTokCookieSetting.updated_at.desc())
    )
    cookie_setting = cookie_res.scalars().first()
    cookie_status = {
        "has_cookie": bool(cookie_setting),
        "expires_at": cookie_setting.expires_at if cookie_setting else None,
        "expiring_soon": False,
        "days_left": None,
    }
    if cookie_setting and cookie_setting.expires_at:
        now = datetime.now(timezone.utc)
        exp = cookie_setting.expires_at
        # Nếu DB trả naive (hiếm), coi là UTC để tránh crash.
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - now
        days_left = int(delta.total_seconds() // 86400)
        cookie_status["days_left"] = days_left
        cookie_status["expiring_soon"] = days_left <= 7

    # Sync runs (manual + cron)
    manual_res = await db.execute(
        select(TikTokSyncRun)
        .where(TikTokSyncRun.kind == "manual")
        .order_by(TikTokSyncRun.started_at.desc())
        .limit(1)
    )
    last_manual = manual_res.scalars().first()
    cron_res = await db.execute(
        select(TikTokSyncRun)
        .where(TikTokSyncRun.kind == "cron")
        .order_by(TikTokSyncRun.started_at.desc())
        .limit(1)
    )
    last_cron = cron_res.scalars().first()

    def _fmt_hcm(dt: Optional[datetime]) -> Optional[str]:
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_HCM_TZ).strftime("%Y-%m-%d %H:%M")

    last_manual_time = _fmt_hcm((last_manual.finished_at if last_manual else None) or (last_manual.started_at if last_manual else None))
    last_cron_time = _fmt_hcm((last_cron.finished_at if last_cron else None) or (last_cron.started_at if last_cron else None))

    schedule_res = await db.execute(
        select(TikTokSyncScheduleSetting).order_by(TikTokSyncScheduleSetting.updated_at.desc())
    )
    schedule_setting = schedule_res.scalars().first()
    schedule_state = {
        "enabled": bool(schedule_setting.enabled) if schedule_setting else False,
        "hour": int(schedule_setting.hour if schedule_setting else 7),
        "minute": int(schedule_setting.minute if schedule_setting else 0),
    }

    # Chuẩn hoá snapshot latest videos để template render dễ (list dài đúng 5)
    import json as _json
    for p in profiles:
        # Format thời gian upload video mới nhất về giờ HCM để UI hiển thị nhất quán
        setattr(p, "last_video_published_at_hcm", _fmt_hcm(getattr(p, "last_video_published_at", None)))

        items = []
        raw = getattr(p, "latest_videos_json", None)
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    items = [x for x in parsed if isinstance(x, dict)]
            except Exception:
                items = []
        if len(items) < 5:
            items = items + [{} for _ in range(5 - len(items))]
        setattr(p, "latest_videos", items[:5])
    
    return templates.TemplateResponse(
        request=request,
        name="tiktok_profiles.html",
        context={
            "profiles": profiles,
            "current_page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "cookie_status": cookie_status,
            "cookie_json_saved": cookie_setting.cookie_json if cookie_setting else "",
            "last_manual_sync": last_manual,
            "last_cron_sync": last_cron,
            "last_manual_sync_time_hcm": last_manual_time,
            "last_cron_sync_time_hcm": last_cron_time,
            "schedule_setting": schedule_state,
            "sort": sort_key,
            "manager_filter": manager_filter,
            "status_filter": status_filter,
            "active_menu": "tiktok"
        }
    )


@app.post(
    "/api/tiktok-cookie",
    dependencies=[Depends(require_login_api)],
)
async def upsert_tiktok_cookie(
    request: Request,
    cookie_json: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    raw = (cookie_json or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="cookie_json is required")
    expires_at = _compute_cookie_expires_at(raw)
    res = await db.execute(select(TikTokCookieSetting).order_by(TikTokCookieSetting.updated_at.desc()))
    setting = res.scalars().first()
    if setting:
        setting.cookie_json = raw
        setting.expires_at = expires_at
    else:
        setting = TikTokCookieSetting(cookie_json=raw, expires_at=expires_at)
        db.add(setting)
    await db.commit()
    return RedirectResponse("/tiktok-profiles", status_code=303)


@app.post(
    "/api/tiktok-cookie/delete",
    dependencies=[Depends(require_login_api)],
)
async def delete_tiktok_cookie(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(TikTokCookieSetting))
    await db.commit()
    return RedirectResponse("/tiktok-profiles", status_code=303)


@app.post(
    "/api/tiktok-profiles/sync",
    dependencies=[Depends(require_login_api)],
)
async def sync_tiktok_profiles(background_tasks: BackgroundTasks):
    """
    Chạy sync để update lại followers + view 5 video mới nhất cho tất cả kênh TikTok.
    Thực thi ở background để không block request.
    """
    async with AsyncSessionLocal() as db:
        run = TikTokSyncRun(kind="manual", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = int(run.id)
    background_tasks.add_task(refresh_all_tiktok_profiles_stats_task_with_run, run_id)
    return {"ok": True, "started": True, "run_id": run_id}


@app.post(
    "/api/tiktok-sync-schedule",
    dependencies=[Depends(require_login_api)],
)
async def upsert_tiktok_sync_schedule(
    enabled: Optional[str] = Form(None),
    hour: int = Form(7),
    minute: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    enabled_bool = bool(enabled)
    try:
        _validate_schedule_time(hour, minute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    res = await db.execute(
        select(TikTokSyncScheduleSetting).order_by(TikTokSyncScheduleSetting.updated_at.desc())
    )
    setting = res.scalars().first()
    if setting:
        setting.enabled = enabled_bool
        setting.hour = hour
        setting.minute = minute
    else:
        setting = TikTokSyncScheduleSetting(
            enabled=enabled_bool,
            hour=hour,
            minute=minute,
        )
        db.add(setting)
    await db.commit()

    # Re-apply runtime scheduler: remove old job and create new job (if enabled).
    await _apply_tiktok_sync_schedule(enabled=enabled_bool, hour=hour, minute=minute)
    return RedirectResponse("/tiktok-profiles", status_code=303)

@app.post("/api/tiktok-profiles/import")
async def import_tiktok_profiles(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api)
):
    content = await file.read()
    decoded = content.decode('utf-8')
    reader = csv.reader(io.StringIO(decoded))
    
    # Assume CSV has headers or not? Let's handle both.
    # User said: "mỗi row là 1 link kênh tiktok và cột note"
    # We'll skip header if it looks like one.
    
    imported_count = 0
    for row in reader:
        if not row or len(row) < 1:
            continue
        
        url = row[0].strip()
        note = row[1].strip() if len(row) > 1 else ""
        
        if not url or url.lower() == "url" or url.lower() == "link":
            continue # Skip header
            
        # Check existence
        stmt = select(TikTokProfile).where(TikTokProfile.url == url)
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            continue

        profile = TikTokProfile(url=url, note=note, followers_count=0)
        db.add(profile)
        imported_count += 1
        
    if imported_count > 0:
        await db.commit()
        
    return {
        "message": f"Successfully imported {imported_count} profiles."
    }

@app.post("/api/tiktok-profiles")
async def add_tiktok_profile(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    stmt = select(TikTokProfile).where(TikTokProfile.url == url)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Profile already exists")

    followers_count = 0
    try:
        followers_count = int(await get_tiktok_followers_count(url))
    except Exception as exc:
        logger.warning("Không lấy được followers cho %s: %s", url, exc)
        followers_count = 0

    profile = TikTokProfile(url=url, note=note, followers_count=followers_count)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    if followers_count == 0:
        background_tasks.add_task(
            refresh_tiktok_profile_followers_task,
            profile.id,
            profile.url,
        )
    return RedirectResponse("/tiktok-profiles", status_code=303)

class TikTokBatchDeleteRequest(BaseModel):
    ids: list[int]


@app.post("/api/tiktok-profiles/batch-delete")
async def batch_delete_tiktok_profiles(
    req: TikTokBatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    if not req.ids:
        raise HTTPException(status_code=400, detail="Không có kênh nào được chọn")

    await db.execute(delete(TikTokProfile).where(TikTokProfile.id.in_(req.ids)))
    await db.commit()
    return {"status": "ok", "deleted_count": len(req.ids)}


@app.post("/api/tiktok-profiles/{profile_id}/delete")
async def delete_tiktok_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api)
):
    await db.execute(delete(TikTokProfile).where(TikTokProfile.id == profile_id))
    await db.commit()
    return RedirectResponse("/tiktok-profiles", status_code=303)



@app.post("/api/tiktok-profiles/{profile_id}/update")
async def update_tiktok_profile(
    profile_id: int,
    url: str = Form(...),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api)
):
    stmt = select(TikTokProfile).where(TikTokProfile.id == profile_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    url = url.strip()
    note = note.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Nếu đổi URL, kiểm tra unique và cập nhật followers (best-effort)
    if profile.url != url:
        exists_stmt = (
            select(TikTokProfile.id)
            .where(TikTokProfile.url == url)
            .where(TikTokProfile.id != profile_id)
        )
        exists = await db.execute(exists_stmt)
        if exists.scalar_one_or_none() is not None:
            raise HTTPException(status_code=400, detail="Profile URL already exists")

        profile.url = url
        try:
            profile.followers_count = int(await get_tiktok_followers_count(url))
        except Exception as exc:
            logger.warning("Không lấy được followers cho %s: %s", url, exc)

    profile.note = note or None
    await db.commit()
    return RedirectResponse("/tiktok-profiles", status_code=303)

@app.post("/api/tiktok-profiles/{profile_id}/upload-status")
async def update_tiktok_profile_upload_status(
    profile_id: int,
    upload_status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    if upload_status not in TIKTOK_PROFILE_UPLOAD_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"upload_status không hợp lệ: {upload_status!r}",
        )
    stmt = select(TikTokProfile).where(TikTokProfile.id == profile_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    profile.upload_status = upload_status
    await db.commit()
    return {"ok": True, "upload_status": upload_status}


@app.post("/api/tiktok-profiles/{profile_id}/manager")
async def update_tiktok_profile_manager(
    profile_id: int,
    manager: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    m = (manager or "").strip().lower()
    if m == "":
        new_value = None
    elif m in {ChannelManager.TUNG.value, ChannelManager.LONG.value}:
        new_value = ChannelManager(m)
    else:
        raise HTTPException(status_code=400, detail="manager không hợp lệ")

    stmt = select(TikTokProfile).where(TikTokProfile.id == profile_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    profile.manager = new_value
    await db.commit()
    return {"ok": True, "profile_id": profile_id, "manager": new_value.value if new_value else None}

@app.get("/youtube-channels")
async def youtube_channels_page(
    request: Request,
    search: Optional[str] = None,
    sort: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    
    page = int(page or 1)
    if page < 1:
        page = 1
    page_size = int(page_size or 50)
    if page_size < 5:
        page_size = 5
    if page_size > 200:
        page_size = 200

    sort_key = (sort or "").strip().lower()
    if sort_key == "created_asc":
        base_stmt = select(YoutubeChannel).order_by(YoutubeChannel.created_at.asc())
    else:
        sort_key = "created_desc"
        base_stmt = select(YoutubeChannel).order_by(YoutubeChannel.created_at.desc())

    search_term = (search or "").strip()
    if search_term:
        base_stmt = base_stmt.where(YoutubeChannel.url.ilike(f"%{search_term}%"))

    total_count_stmt = select(func.count()).select_from(YoutubeChannel)
    if search_term:
        total_count_stmt = total_count_stmt.where(YoutubeChannel.url.ilike(f"%{search_term}%"))

    total_count_res = await db.execute(total_count_stmt)
    total_count = int(total_count_res.scalar() or 0)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size

    stmt = base_stmt.limit(page_size).offset(offset)
    result = await db.execute(stmt)
    channels = result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="youtube_channels.html",
        context={
            "channels": channels,
            "current_page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "search": search_term,
            "sort": sort_key,
            "active_menu": "youtube",
        }
    )

async def youtube_subscribe_task(channel_db_id: int, channel_url: str):
    # Resolve channel_id (UC...)
    channel_id = await get_youtube_channel_id(channel_url)
    if not channel_id:
        logger.error(f"Could not resolve YouTube Channel ID for URL: {channel_url}")
        return

    async with AsyncSessionLocal() as db:
        # Update db record
        await db.execute(
            update(YoutubeChannel)
            .where(YoutubeChannel.id == channel_db_id)
            .values(channel_id=channel_id)
        )
        await db.commit()

        # If CALLBACK_BASE_URL is set, subscribe to WebSub
        callback_base = os.getenv("CALLBACK_BASE_URL", "").strip()
        if callback_base:
            await subscribe_youtube_pubsub(channel_id, callback_base, mode="subscribe")
        else:
            logger.warning("CALLBACK_BASE_URL environment variable is not set; skipping YouTube WebSub subscription")

async def process_youtube_websub_video(channel_id: str, video_id: str):
    """
    Background task to update the channel's last_video_id and trigger external API
    for both YouTube Shorts and regular videos.
    """
    # Verify if the video is a Short to determine correct URL format
    is_short = await is_youtube_short(video_id)
    logger.info(f"WebSub validation check: video {video_id} is_short={is_short}")

    video_url = f"https://www.youtube.com/shorts/{video_id}" if is_short else f"https://www.youtube.com/watch?v={video_id}"

    # Update the DB where channel_id matches
    async with AsyncSessionLocal() as db:
        stmt = select(YoutubeChannel).where(YoutubeChannel.channel_id == channel_id)
        res = await db.execute(stmt)
        channel = res.scalars().first()
        
        if channel:
            channel.last_video_id = video_id
            await db.commit()
            logger.info(f"Successfully updated YouTube channel {channel.url} last_video_id to {video_id}")
            
            # If ngrok_url is configured, trigger the external API call
            if channel.ngrok_url:
                ngrok_target = channel.ngrok_url.rstrip("/")
                api_url = f"{ngrok_target}/api/upload_new_video"
                logger.info(f"Kênh {channel.url} có ngrok_url, đang gọi API ngoài: {api_url}")
                
                import httpx
                payload = {
                    "channel_id": channel_id,
                    "channel_url": channel.url,
                    "video_id": video_id,
                    "video_url": video_url
                }
                
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(api_url, json=payload)
                        logger.info(f"API ngrok callback response: status={response.status_code}, body={response.text[:200]}")
                except Exception as ex:
                    logger.error(f"Lỗi khi gọi API ngrok ngoài {api_url}: {ex}")
        else:
            logger.warning(f"No YouTube channel entry found in DB with channel_id={channel_id}")

class YoutubeBulkUpdateNgrokRequest(BaseModel):
    ids: list[int]
    ngrok_url: Optional[str] = None

@app.post("/api/youtube-channels")
async def add_youtube_channel(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    last_video_id: Optional[str] = Form(None),
    ngrok_url: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Channel URL is required")
    
    last_video_id = (last_video_id or "").strip() or None
    ngrok_url = (ngrok_url or "").strip() or None

    # Check unique url
    stmt = select(YoutubeChannel).where(YoutubeChannel.url == url)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Kênh YouTube đã tồn tại")

    channel = YoutubeChannel(url=url, last_video_id=last_video_id, ngrok_url=ngrok_url)
    db.add(channel)
    await db.commit()
    await db.refresh(channel)

    # Queue background task to resolve Channel ID and subscribe
    background_tasks.add_task(youtube_subscribe_task, channel.id, channel.url)

    return RedirectResponse("/youtube-channels", status_code=303)

@app.post("/api/youtube-channels/{channel_id}/update")
async def update_youtube_channel(
    channel_id: int,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    last_video_id: Optional[str] = Form(None),
    ngrok_url: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    stmt = select(YoutubeChannel).where(YoutubeChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Không tìm thấy kênh YouTube")

    last_video_id = (last_video_id or "").strip() or None
    ngrok_url = (ngrok_url or "").strip() or None
    url_changed = channel.url != url

    if url_changed:
        exists_stmt = select(YoutubeChannel.id).where(YoutubeChannel.url == url).where(YoutubeChannel.id != channel_id)
        exists = await db.execute(exists_stmt)
        if exists.scalar_one_or_none() is not None:
            raise HTTPException(status_code=400, detail="Kênh YouTube đã tồn tại với URL này")
        channel.url = url
        channel.channel_id = None # Reset it so the background task resolves it again

    channel.last_video_id = last_video_id
    channel.ngrok_url = ngrok_url
    await db.commit()

    if url_changed:
        background_tasks.add_task(youtube_subscribe_task, channel_id, url)

    return RedirectResponse("/youtube-channels", status_code=303)

@app.post("/api/youtube-channels/bulk-update-ngrok")
async def bulk_update_youtube_channels_ngrok(
    req: YoutubeBulkUpdateNgrokRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    if not req.ids:
        raise HTTPException(status_code=400, detail="Không có kênh nào được chọn")
    
    cleaned_url = (req.ngrok_url or "").strip() or None
    
    # Update in bulk
    stmt = (
        update(YoutubeChannel)
        .where(YoutubeChannel.id.in_(req.ids))
        .values(ngrok_url=cleaned_url)
    )
    await db.execute(stmt)
    await db.commit()
    return {"ok": True, "updated_count": len(req.ids)}


@app.post("/api/youtube-channels/{channel_id}/delete")
async def delete_youtube_channel(
    channel_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(require_login_api),
):
    stmt = select(YoutubeChannel).where(YoutubeChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel:
        yt_channel_id = channel.channel_id
        callback_base = os.getenv("CALLBACK_BASE_URL", "").strip()
        if yt_channel_id and callback_base:
            background_tasks.add_task(subscribe_youtube_pubsub, yt_channel_id, callback_base, "unsubscribe")

        await db.delete(channel)
        await db.commit()

    return RedirectResponse("/youtube-channels", status_code=303)

@app.get("/api/youtube/pubsub")
async def youtube_pubsub_verify(
    request: Request,
):
    """
    YouTube WebSub (PubSubHubbub) Verification of Intent callback handler.
    YouTube sends GET challenge to verify active subscription.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    topic = params.get("hub.topic")
    challenge = params.get("hub.challenge")
    lease = params.get("hub.lease_seconds")

    logger.info(f"YouTube WebSub verification request received: mode={mode}, topic={topic}, lease={lease}")

    if mode in ("subscribe", "unsubscribe") and challenge:
        from fastapi.responses import Response
        return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=400, detail="Invalid verification request")

@app.post("/api/youtube/pubsub")
async def youtube_pubsub_callback(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    YouTube WebSub (PubSubHubbub) feed notification endpoint.
    Receives XML data when a new video is published.
    """
    body = await request.body()
    logger.info(f"YouTube WebSub feed notification received: {len(body)} bytes")

    try:
        import xml.etree.ElementTree as ET
        
        root = ET.fromstring(body)
        
        namespaces = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015"
        }
        
        entry = root.find("atom:entry", namespaces)
        if entry is None:
            entry = root.find("entry")
            
        if entry is not None:
            video_id_el = entry.find("yt:videoId", namespaces)
            if video_id_el is None:
                video_id_el = entry.find("videoId")
                
            channel_id_el = entry.find("yt:channelId", namespaces)
            if channel_id_el is None:
                channel_id_el = entry.find("channelId")
                
            if video_id_el is not None and channel_id_el is not None:
                video_id = video_id_el.text.strip()
                channel_id = channel_id_el.text.strip()
                
                logger.info(f"WebSub parsed new video: {video_id} for channel: {channel_id}")
                
                background_tasks.add_task(process_youtube_websub_video, channel_id, video_id)
            else:
                logger.warning("WebSub XML entry missing videoId or channelId")
        else:
            logger.warning("WebSub XML missing entry tag")

    except Exception as e:
        logger.exception("Error processing YouTube WebSub XML notification payload")

    from fastapi.responses import Response
    return Response(status_code=204)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

