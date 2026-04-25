from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from playwright.async_api import async_playwright
from yt_dlp.utils import YoutubeDLError

logger = logging.getLogger(__name__)

# Resolve cookie.json from project root (not cwd — uvicorn may start elsewhere).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_COOKIE_PATH = _PROJECT_ROOT / "cookie.json"


def _chrome_extension_json_to_netscape(cookie_json_text: str) -> str:
    """Chrome extension 'Get cookies.txt' JSON export → Netscape format for yt-dlp."""
    data = json.loads(cookie_json_text)
    lines = ["# Netscape HTTP Cookie File", "# Converted for yt-dlp"]
    for c in data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = c.get("domain") or ""
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expirationDate")
        if c.get("session") or exp is None:
            expiry = "0"
        else:
            try:
                expiry = str(int(float(exp)))
            except (TypeError, ValueError):
                expiry = "0"
        name, value = c["name"], c.get("value", "")
        lines.append(
            f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"
        )
    return "\n".join(lines)


def _load_tiktok_cookies_for_playwright() -> list[dict[str, Any]]:
    """
    Đọc cookie.json (export từ extension) và convert sang format Playwright.
    Chỉ lấy cookie thuộc domain tiktok.com để giảm rủi ro lỗi.
    """
    cookie_json = Path(os.getenv("TIKTOK_COOKIES_FILE", str(_DEFAULT_COOKIE_PATH))).expanduser()
    if not cookie_json.is_file():
        return []
    try:
        raw = cookie_json.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("TikTok cookies: không đọc/parse được cookie.json (%s)", exc)
        return []

    cookies: list[dict[str, Any]] = []
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value", "")
        domain = c.get("domain") or ""
        if not name or "tiktok.com" not in domain:
            continue
        ck: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
        }
        exp = c.get("expirationDate")
        if exp and not c.get("session"):
            try:
                ck["expires"] = float(exp)
            except Exception:
                pass
        cookies.append(ck)
    return cookies


async def get_youtube_videos(channel_url: str):
    videos = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        
        # Ensure url ends with /shorts
        if not channel_url.endswith("/shorts"):
            channel_url = channel_url.rstrip("/") + "/shorts"
            
        print(f"Scraping YouTube Shorts from: {channel_url}")
        await page.goto(channel_url, wait_until="networkidle")
        
        try:
            # Wait for any shorts link to appear
            await page.wait_for_selector('a[href^="/shorts/"]', timeout=20000)
        except:
            print("Timeout waiting for shorts links")
            await page.screenshot(path="debug_youtube.png")
            await browser.close()
            return []

        # Scroll to load all shorts
        while True:
            last_height = await page.evaluate("document.documentElement.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await asyncio.sleep(3) # Wait for content to load
            new_height = await page.evaluate("document.documentElement.scrollHeight")
            if new_height == last_height:
                # Try one more time just in case of slow loading
                await asyncio.sleep(2)
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                if await page.evaluate("document.documentElement.scrollHeight") == last_height:
                    break
            
        # Extract shorts elements
        shorts_links = await page.query_selector_all('a[href^="/shorts/"]')
        print(f"Found {len(shorts_links)} candidate links")
        seen_urls = set()
        for link in shorts_links:
            url = await link.get_attribute("href")
            if url and len(url.strip("/")) > 7 and url not in seen_urls:
                seen_urls.add(url)
                full_url = f"https://www.youtube.com{url}"
                videos.append({
                    "url": full_url,
                    "upload_date": datetime.now()
                })
        
        print(f"Successfully extracted {len(videos)} unique shorts")
                
        await browser.close()
    return videos

def _tiktok_entry_video_url(ent: dict[str, Any], channel_url: str) -> Optional[str]:
    """Lấy URL video chuẩn từ entry flat của yt-dlp (có thể thiếu field `url` ở một số phiên bản)."""
    raw = ent.get("url") or ent.get("webpage_url") or ""
    if raw and "/video/" in raw:
        return raw.split("?")[0]
    vid = ent.get("id")
    base = (ent.get("uploader_url") or channel_url).split("?")[0].rstrip("/")
    if vid and str(vid).isdigit() and "/@" in base:
        return f"{base}/video/{vid}"
    return None


async def get_tiktok_videos(channel_url: str):
    """
    TikTok Web returns empty JSON for item_list without browser X-Bogus/X-Gnarly signatures.
    yt-dlp with Netscape cookies (from the same cookie.json as Playwright) reliably lists videos.
    """
    channel_url = channel_url.strip()
    cookie_json = Path(os.getenv("TIKTOK_COOKIES_FILE", str(_DEFAULT_COOKIE_PATH))).expanduser()
    tmp_cookie: Optional[Path] = None

    def _build_tmp_cookie_from_json() -> Optional[Path]:
        if not cookie_json.is_file():
            logger.info(
                "TikTok: không có cookie tại %s — thử yt-dlp không cookie "
                "(export cookie.json hoặc đặt TIKTOK_COOKIES_FILE)",
                cookie_json,
            )
            return None
        try:
            text = cookie_json.read_text(encoding="utf-8")
            netscape = _chrome_extension_json_to_netscape(text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "TikTok: file cookie không đọc/parse được (%s), bỏ qua cookie và thử không cookie",
                exc,
            )
            return None
        fd, tmp_name = tempfile.mkstemp(prefix="tiktok_ytdlp_", suffix=".txt", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(netscape)
        src = str(cookie_json.resolve())
        logger.info(
            "TikTok: yt-dlp dùng cookie đã chuyển từ %s (%s dòng)",
            src,
            netscape.count("\n") + 1,
        )
        return Path(tmp_name)

    try:
        tmp_cookie = _build_tmp_cookie_from_json()

        playlist_end = int(os.getenv("TIKTOK_YTDLP_PLAYLIST_END", "2000"))

        def _extract(cookiefile: Optional[str]):
            opts: dict = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
                "playlistend": playlist_end,
                "skip_download": True,
                "ignoreerrors": True,
            }
            if cookiefile:
                opts["cookiefile"] = cookiefile
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(channel_url, download=False)

        def _parse_entries(info: Optional[dict]) -> list[dict[str, Any]]:
            if not info:
                return []
            if info.get("_type") == "playlist":
                return [e for e in (info.get("entries") or []) if e]
            if info.get("url"):
                return [info]
            return []

        cookie_path_str = str(tmp_cookie) if tmp_cookie is not None else None
        try:
            info = await asyncio.to_thread(_extract, cookie_path_str)
        except (YoutubeDLError, OSError) as first_err:
            if cookie_path_str:
                logger.warning(
                    "TikTok: yt-dlp lỗi khi có cookie (%s), thử lại không cookie",
                    first_err,
                )
                if tmp_cookie is not None:
                    try:
                        tmp_cookie.unlink(missing_ok=True)
                    except OSError:
                        pass
                    tmp_cookie = None
                info = await asyncio.to_thread(_extract, None)
            else:
                raise

        entries = _parse_entries(info)

        videos = []
        seen = set()
        for ent in entries:
            clean = _tiktok_entry_video_url(ent, channel_url)
            if not clean:
                continue
            if clean in seen:
                continue
            seen.add(clean)
            ts = ent.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 0:
                # Một số bản trả timestamp ms
                if ts > 1e12:
                    ts = ts / 1000.0
                upload = datetime.fromtimestamp(int(ts))
            else:
                upload = datetime.now()
            videos.append({"url": clean, "upload_date": upload})

        logger.info("TikTok yt-dlp: %s video từ %r", len(videos), channel_url)
        return videos
    finally:
        if tmp_cookie is not None:
            try:
                tmp_cookie.unlink(missing_ok=True)
            except OSError:
                pass


def _parse_tiktok_follower_count_from_html(html: str) -> Optional[int]:
    """
    Parse followerCount từ HTML TikTok profile page.
    TikTok thường embed JSON có key "followerCount".
    """
    if not html:
        return None
    # Lấy tất cả followerCount xuất hiện và chọn số lớn nhất để tránh trúng field phụ.
    # Ví dụ: "followerCount":123456
    import re

    matches = re.findall(r'"followerCount"\s*:\s*(\d+)', html)
    if not matches:
        return None
    try:
        nums = [int(x) for x in matches]
    except ValueError:
        return None
    return max(nums) if nums else None


async def get_tiktok_followers_count(profile_url: str) -> int:
    """
    Lấy số follower của 1 kênh TikTok từ URL profile.
    - Dùng Playwright vì TikTok nhiều khi cần JS để render.
    - Trả về 0 nếu không parse được.
    """
    profile_url = (profile_url or "").strip()
    if not profile_url:
        return 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1365, "height": 768},
        )
        # Add cookies nếu có (giúp tránh bị TikTok chặn / trả trang rỗng).
        try:
            cookies = _load_tiktok_cookies_for_playwright()
            if cookies:
                await context.add_cookies(cookies)
        except Exception as exc:
            logger.warning("TikTok cookies: add_cookies failed (%s)", exc)
        page = await context.new_page()
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            # Đợi thêm một chút để JS hydrate (TikTok thường render muộn).
            await page.wait_for_timeout(2500)
            html = await page.content()
            parsed = _parse_tiktok_follower_count_from_html(html)
            if parsed is not None:
                return int(parsed)
            # Fallback: thử đọc innerText để bắt trường hợp TikTok render theo text.
            body_text = await page.inner_text("body")
            parsed2 = _parse_tiktok_follower_count_from_html(body_text)
            return int(parsed2 or 0)
        finally:
            await context.close()
            await browser.close()
