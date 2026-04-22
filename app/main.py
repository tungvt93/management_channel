import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .database import AsyncSessionLocal, get_db, init_db
from .models import Channel, Platform, ScrapingStatus, User, VideoLink, VideoStatus
from .scraper import get_tiktok_videos, get_youtube_videos

logger = logging.getLogger(__name__)

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


async def require_login_api(request: Request) -> None:
    if request.session.get("user_id") is None:
        raise HTTPException(status_code=401, detail="Not authenticated")


# Pydantic Schemas
class ChannelCreate(BaseModel):
    url: str

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

# 1. API: Get list of available video links
@app.get(
    "/api/videos",
    response_model=List[VideoResponse],
    dependencies=[Depends(require_login_api)],
)
async def get_available_videos(channel_link: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    stmt = select(VideoLink).where(VideoLink.status == VideoStatus.AVAILABLE)
    if channel_link:
        stmt = stmt.join(Channel).where(Channel.url == channel_link)
    result = await db.execute(stmt)
    return result.scalars().all()

# 2. API: Update video status
@app.patch(
    "/api/videos/{video_id}",
    response_model=VideoResponse,
    dependencies=[Depends(require_login_api)],
)
async def update_video_status(video_id: int, status_update: VideoUpdate, db: AsyncSession = Depends(get_db)):
    stmt = update(VideoLink).where(VideoLink.id == video_id).values(status=status_update.status).returning(VideoLink)
    result = await db.execute(stmt)
    updated_video = result.scalar_one_or_none()
    
    if not updated_video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    await db.commit()
    return updated_video

# 3. API: Add channel and trigger scrape
@app.post("/api/channels", dependencies=[Depends(require_login_api)])
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


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
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
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    page_size = 20
    offset = (page - 1) * page_size

    # Fetch all channels for the filter dropdown
    channels_result = await db.execute(select(Channel).order_by(Channel.created_at.desc()))
    channels = channels_result.scalars().all()
    
    # Build video query with optional filter
    video_stmt = select(VideoLink).order_by(VideoLink.upload_date.desc(), VideoLink.created_at.desc())
    count_stmt = select(func.count()).select_from(VideoLink)

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
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "channels": channels,
        "videos": videos,
        "selected_channel": channel_id,
        "current_page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "Platform": Platform,
        "VideoStatus": VideoStatus
    })

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
