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
_PLAYWRIGHT_STABLE_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

# Giới hạn số Playwright/Chromium chạy đồng thời (cron + import CSV + API đều gọi scraper).
_playwright_semaphore: Optional[asyncio.Semaphore] = None


def _get_playwright_semaphore() -> asyncio.Semaphore:
    global _playwright_semaphore
    if _playwright_semaphore is None:
        n = max(1, int(os.getenv("PLAYWRIGHT_MAX_CONCURRENT", "1")))
        _playwright_semaphore = asyncio.Semaphore(n)
    return _playwright_semaphore


def _is_tiktok_no_videos_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return (
        "does not have any videos posted" in msg
        or "this account does not have any videos posted" in msg
        or "no videos posted" in msg
    )


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


def _load_tiktok_cookies_for_playwright(cookie_json_text: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Đọc cookie.json (export từ extension) và convert sang format Playwright.
    Chỉ lấy cookie thuộc domain tiktok.com để giảm rủi ro lỗi.
    """
    try:
        if cookie_json_text is None:
            cookie_json = Path(os.getenv("TIKTOK_COOKIES_FILE", str(_DEFAULT_COOKIE_PATH))).expanduser()
            if not cookie_json.is_file():
                return []
            cookie_json_text = cookie_json.read_text(encoding="utf-8")
        data = json.loads(cookie_json_text)
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


def _read_cookie_json_text(cookie_json_text: Optional[str]) -> Optional[str]:
    if cookie_json_text is not None:
        return cookie_json_text
    cookie_json = Path(os.getenv("TIKTOK_COOKIES_FILE", str(_DEFAULT_COOKIE_PATH))).expanduser()
    if not cookie_json.is_file():
        return None
    try:
        return cookie_json.read_text(encoding="utf-8")
    except Exception:
        return None


async def get_youtube_videos(channel_url: str):
    videos = []
    async with _get_playwright_semaphore():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=_PLAYWRIGHT_STABLE_ARGS)
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
                # Handle YouTube cookie consent/redirect page if it appears (common in headless/Docker environments)
                consent_selector = 'button[aria-label*="Accept all"], button[aria-label*="Accept the use"], button[aria-label*="Đồng ý"], button:has-text("Accept all"), button:has-text("Tôi đồng ý"), button:has-text("Accept")'
                consent_btn = page.locator(consent_selector).first
                if await consent_btn.is_visible(timeout=3000):
                    await consent_btn.click()
                    print("Clicked YouTube cookie consent button")
                    await page.wait_for_load_state("networkidle")
            except Exception as e:
                pass

            try:
                # Wait for any shorts link to appear
                await page.wait_for_selector('a[href^="/shorts/"]', timeout=20000)
            except Exception as e:
                print("Timeout waiting for shorts links")
                await page.screenshot(path="debug_youtube.png")
                await browser.close()
                raise TimeoutError("Timeout waiting for shorts links. Maybe blocked by YouTube bot protection or a cookie consent dialog.") from e

            # Scroll to load all shorts
            while True:
                last_height = await page.evaluate("document.documentElement.scrollHeight")
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                await asyncio.sleep(3)  # Wait for content to load
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


async def get_tiktok_videos(channel_url: str, cookie_json_text: Optional[str] = None):
    """
    TikTok Web returns empty JSON for item_list without browser X-Bogus/X-Gnarly signatures.
    yt-dlp with Netscape cookies (from the same cookie.json as Playwright) reliably lists videos.
    """
    channel_url = channel_url.strip()
    cookie_text = _read_cookie_json_text(cookie_json_text)
    tmp_cookie: Optional[Path] = None

    def _build_tmp_cookie_from_json() -> Optional[Path]:
        if not cookie_text:
            logger.info(
                "TikTok: không có cookie — thử yt-dlp không cookie "
                "(cập nhật cookie ở Settings hoặc mount cookie.json/TIKTOK_COOKIES_FILE)",
            )
            return None
        try:
            netscape = _chrome_extension_json_to_netscape(cookie_text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "TikTok: file cookie không đọc/parse được (%s), bỏ qua cookie và thử không cookie",
                exc,
            )
            return None
        fd, tmp_name = tempfile.mkstemp(prefix="tiktok_ytdlp_", suffix=".txt", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(netscape)
        logger.info("TikTok: yt-dlp dùng cookie (%s dòng)", netscape.count("\n") + 1)
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

        def _extract_allow_empty(cookiefile: Optional[str]):
            try:
                return _extract(cookiefile)
            except (YoutubeDLError, OSError) as exc:
                if _is_tiktok_no_videos_error(exc):
                    logger.info(
                        "TikTok yt-dlp: tài khoản không có video, trả danh sách rỗng (%s)",
                        channel_url,
                    )
                    return None
                raise

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
            info = await asyncio.to_thread(_extract_allow_empty, cookie_path_str)
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
                info = await asyncio.to_thread(_extract_allow_empty, None)
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


def _tiktok_entry_views(ent: dict[str, Any]) -> int:
    """
    yt-dlp có thể trả view count dưới nhiều key tuỳ extractor/version.
    Chuẩn hoá về int >= 0.
    """
    for k in ("view_count", "views", "play_count", "playCount", "stats", "statistics"):
        v = ent.get(k)
        if isinstance(v, (int, float)):
            return max(0, int(v))
        if isinstance(v, dict):
            for kk in ("viewCount", "playCount", "views", "view_count"):
                vv = v.get(kk)
                if isinstance(vv, (int, float)):
                    return max(0, int(vv))
    return 0


async def get_tiktok_latest_videos_with_views(
    profile_url: str, limit: int = 5, cookie_json_text: Optional[str] = None
) -> list[dict[str, Any]]:
    """
    Lấy danh sách video mới nhất (mặc định 5) kèm view count.
    Dùng yt-dlp (không extract_flat) để lấy metadata của từng entry.
    """
    profile_url = (profile_url or "").strip()
    if not profile_url or limit <= 0:
        return []

    cookie_text = _read_cookie_json_text(cookie_json_text)
    tmp_cookie: Optional[Path] = None

    def _build_tmp_cookie_from_json() -> Optional[Path]:
        if not cookie_text:
            logger.info("TikTok: không có cookie (latest_videos_with_views) — thử không cookie")
            return None
        try:
            netscape = _chrome_extension_json_to_netscape(cookie_text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError, KeyError, ValueError) as exc:
            logger.warning(
                "TikTok: cookie không đọc/parse được (latest_videos_with_views), bỏ qua cookie: %s",
                exc,
            )
            return None
        fd, tmp_name = tempfile.mkstemp(prefix="tiktok_ytdlp_", suffix=".txt", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(netscape)
        logger.info("TikTok: yt-dlp (latest_videos_with_views) dùng cookie (%s dòng)", netscape.count("\n") + 1)
        return Path(tmp_name)

    def _extract(cookiefile: Optional[str]):
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # lấy tối đa 30 entry để sort theo timestamp rồi cắt limit (an toàn khi playlist order không ổn định)
            "playlistend": max(30, limit),
            "ignoreerrors": True,
        }
        if cookiefile:
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(profile_url, download=False)

    def _extract_allow_empty(cookiefile: Optional[str]):
        try:
            return _extract(cookiefile)
        except (YoutubeDLError, OSError) as exc:
            if _is_tiktok_no_videos_error(exc):
                logger.info(
                    "TikTok yt-dlp latest: tài khoản không có video, trả danh sách rỗng (%s)",
                    profile_url,
                )
                return None
            raise

    def _parse_entries(info: Optional[dict]) -> list[dict[str, Any]]:
        if not info:
            return []
        if info.get("_type") == "playlist":
            return [e for e in (info.get("entries") or []) if isinstance(e, dict)]
        if isinstance(info, dict):
            return [info]
        return []

    try:
        tmp_cookie = _build_tmp_cookie_from_json()
        cookie_path_str = str(tmp_cookie) if tmp_cookie is not None else None
        try:
            info = await asyncio.to_thread(_extract_allow_empty, cookie_path_str)
        except (YoutubeDLError, OSError):
            if cookie_path_str:
                if tmp_cookie is not None:
                    try:
                        tmp_cookie.unlink(missing_ok=True)
                    except OSError:
                        pass
                    tmp_cookie = None
                info = await asyncio.to_thread(_extract_allow_empty, None)
            else:
                raise

        entries = _parse_entries(info)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx, ent in enumerate(entries):
            url = _tiktok_entry_video_url(ent, profile_url) or (ent.get("webpage_url") or ent.get("url") or "")
            if not url:
                continue
            url = str(url).split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            ts = ent.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 0:
                if ts > 1e12:
                    ts = ts / 1000.0
                ts_int = int(ts)
            else:
                ts_int = 0
            items.append(
                {
                    "url": url,
                    "views": _tiktok_entry_views(ent),
                    "timestamp": ts_int,
                    "_idx": idx,
                }
            )

        # sort mới nhất lên đầu: ưu tiên timestamp desc, fallback theo idx asc (entry đầu thường là mới hơn)
        items.sort(
            key=lambda x: (
                -int(x.get("timestamp") or 0),
                int(x.get("_idx") or 0),
            )
        )
        for it in items:
            it.pop("_idx", None)
        return items[:limit]
    finally:
        if tmp_cookie is not None:
            try:
                tmp_cookie.unlink(missing_ok=True)
            except OSError:
                pass


def _parse_tiktok_follower_count_from_html(html: str) -> Optional[int]:
    """
    Parse followerCount từ HTML TikTok profile page.
    TikTok thường embed JSON có key "followerCount" hoặc "follower_count".
    """
    if not html:
        return None
    import re

    # Ví dụ: "followerCount":123456 hoặc 'follower_count': 123456
    patterns = (
        r'"followerCount"\s*:\s*(\d+)',
        r"'followerCount'\s*:\s*(\d+)",
        r'"follower_count"\s*:\s*(\d+)',
        r"'follower_count'\s*:\s*(\d+)",
    )
    all_nums: list[int] = []
    for pat in patterns:
        for x in re.findall(pat, html):
            try:
                all_nums.append(int(x))
            except ValueError:
                continue
    return max(all_nums) if all_nums else None


def _parse_follower_display_text(text: str) -> Optional[int]:
    """
    Parse số follower từ text UI TikTok: '1.2M', '500K', '830,3K', '1,234,567'.
    """
    import re

    if not text:
        return None
    raw = text.strip().replace("\u00a0", "").replace(" ", "")
    if not raw:
        return None
    low = raw.lower().replace("followers", "").replace("follower", "").strip()
    for suf, mult in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if low.endswith(suf):
            nump = low[: -len(suf)].strip()
            if not nump:
                return None
            # Một dấu phẩy duy nhất thường là thập phân (locale EU); còn lại coi là phân cách nghìn
            if nump.count(",") == 1 and nump.count(".") == 0:
                nump = nump.replace(",", ".")
            else:
                nump = nump.replace(",", "")
            try:
                return int(round(float(nump) * mult))
            except ValueError:
                return None
    digits = re.sub(r"[^\d]", "", raw)
    if digits:
        return int(digits)
    return None


async def _tiktok_followers_from_dom(page: Any) -> Optional[int]:
    """Đọc số follower từ phần tử TikTok render (ổn định hơn khi JSON không còn trong HTML)."""
    selectors = (
        'strong[data-e2e="followers-count"]',
        '[data-e2e="followers-count"]',
    )
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=15_000, state="attached")
        except Exception:
            continue
        el = await page.query_selector(sel)
        if not el:
            continue
        try:
            text = await el.text_content()
        except Exception:
            continue
        n = _parse_follower_display_text(text or "")
        if n is not None and n >= 0:
            return n
    return None


async def get_tiktok_followers_count(profile_url: str, cookie_json_text: Optional[str] = None) -> int:
    """
    Lấy số follower của 1 kênh TikTok từ URL profile.
    - Dùng Playwright vì TikTok nhiều khi cần JS để render.
    - Trả về 0 nếu không parse được.
    """
    profile_url = (profile_url or "").strip()
    if not profile_url:
        return 0

    async with _get_playwright_semaphore():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=_PLAYWRIGHT_STABLE_ARGS)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                viewport={"width": 1365, "height": 768},
            )
            # Add cookies nếu có (giúp tránh bị TikTok chặn / trả trang rỗng).
            try:
                cookies = _load_tiktok_cookies_for_playwright(cookie_json_text=cookie_json_text)
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
                # Fallback DOM: TikTok hay ẩn JSON nhưng vẫn render metric trên UI.
                dom_n = await _tiktok_followers_from_dom(page)
                if dom_n is not None:
                    return int(dom_n)
                # Fallback: innerText body rồi grep JSON lần nữa.
                body_text = await page.inner_text("body")
                parsed2 = _parse_tiktok_follower_count_from_html(body_text)
                if parsed2 is not None:
                    return int(parsed2)
                return 0
            finally:
                await context.close()
                await browser.close()
