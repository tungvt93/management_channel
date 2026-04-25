import pandas as pd
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

EXCEL_FILE = "tiktok_channels.xlsx"
SHEET_NAME = 0  # hoặc tên sheet
URL_COLUMN = "TikTok URL"

async def get_follower_count(page, url):
    try:
        await page.goto(url, timeout=60000)
        await page.wait_for_selector('strong[data-e2e="followers-count"]', timeout=10000)
        el = await page.query_selector('strong[data-e2e="followers-count"]')
        text = await el.text_content()
        return text.strip()
    except Exception as e:
        print(f"❌ Error at {url}: {e}")
        return "N/A"

async def update_followers(df):
    today_str = datetime.today().strftime("%Y-%m-%d")

    # Không cập nhật nếu đã có cột hôm nay
    if today_str in df.columns:
        print(f"⏭️ Đã có dữ liệu ngày {today_str}, bỏ qua...")
        return df

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        results = []
        for url in df[URL_COLUMN]:
            print(f"📦 Fetching {url}...")
            followers = await get_follower_count(page, url)
            results.append(followers)

        await browser.close()

    df[today_str] = results
    return df

def main():
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
    if URL_COLUMN not in df.columns:
        print(f"❌ Cột '{URL_COLUMN}' không tồn tại.")
        return

    updated_df = asyncio.run(update_followers(df))
    updated_df.to_excel(EXCEL_FILE, index=False)
    print(f"✅ Đã cập nhật file: {EXCEL_FILE}")

if __name__ == "__main__":
    main()
