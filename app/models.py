from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum as SQLEnum, Text, text
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()

class Platform(str, enum.Enum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"

class VideoStatus(str, enum.Enum):
    AVAILABLE = "available"
    HOLDED = "holded"
    DONE = "done"

class ScrapingStatus(str, enum.Enum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"

class ChannelManager(str, enum.Enum):
    TUNG = "tung"
    LONG = "long"


# Trạng thái upload (tab TikTok Channels) — giá trị lưu DB
TIKTOK_PROFILE_UPLOAD_PENDING = "cho_up"  # Chờ up
TIKTOK_PROFILE_UPLOAD_IN_PROGRESS = "dang_up"  # Đang up
TIKTOK_PROFILE_UPLOAD_ENABLED = "da_bat"  # Đã bật
TIKTOK_PROFILE_UPLOAD_SOLD = "da_ban"  # Đã bán
TIKTOK_PROFILE_UPLOAD_STATUSES = frozenset(
    {
        TIKTOK_PROFILE_UPLOAD_PENDING,
        TIKTOK_PROFILE_UPLOAD_IN_PROGRESS,
        TIKTOK_PROFILE_UPLOAD_ENABLED,
        TIKTOK_PROFILE_UPLOAD_SOLD,
    }
)


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True, nullable=False)
    platform = Column(SQLEnum(Platform, name="platform", values_callable=lambda x: [e.value for e in x]), nullable=False)
    name = Column(String, nullable=True) # Optional channel name
    manager = Column(
        SQLEnum(
            ChannelManager,
            name="channelmanager",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=True,
    )
    scraping_status = Column(SQLEnum(ScrapingStatus, name="scrapingstatus", values_callable=lambda x: [e.value for e in x]), default=ScrapingStatus.IDLE, nullable=False, server_default=ScrapingStatus.IDLE.value)
    last_scraped_at = Column(DateTime(timezone=True), nullable=True)
    scraping_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    videos = relationship("VideoLink", back_populates="channel", cascade="all, delete-orphan")

class VideoLink(Base):
    __tablename__ = "video_links"

    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    url = Column(String, unique=True, index=True, nullable=False)
    status = Column(SQLEnum(VideoStatus, name="videostatus", values_callable=lambda x: [e.value for e in x]), default=VideoStatus.AVAILABLE, nullable=False)
    upload_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    channel = relationship("Channel", back_populates="videos")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class TikTokProfile(Base):
    __tablename__ = "tiktok_profiles"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True, nullable=False)
    followers_count = Column(Integer, default=0)
    # JSON text: [{"url": "...", "views": 123, "timestamp": 1710000000}, ...] (mới nhất trước)
    latest_videos_json = Column(Text, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    manager = Column(
        SQLEnum(
            ChannelManager,
            name="channelmanager",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=True,
    )
    upload_status = Column(
        String(32),
        nullable=False,
        server_default=text("'cho_up'"),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TikTokCookieSetting(Base):
    __tablename__ = "tiktok_cookie_settings"

    id = Column(Integer, primary_key=True, index=True)
    cookie_json = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TikTokSyncRun(Base):
    __tablename__ = "tiktok_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(32), nullable=False)  # manual | cron
    status = Column(String(32), nullable=False, server_default=text("'running'"))  # running | success | failed
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    message = Column(Text, nullable=True)
