import asyncio
from app.scraper import get_tiktok_videos
import json

async def main():
    url = "https://www.tiktok.com/@user1057495745111"
    print(f"Testing scraper with URL: {url}")
    videos = await get_tiktok_videos(url)
    print(f"Found {len(videos)} videos")
    for v in videos:
        print(f"- {v['url']} ({v['upload_date']})")

if __name__ == "__main__":
    asyncio.run(main())
