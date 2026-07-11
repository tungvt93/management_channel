import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import concurrent.futures
from app.main import _run_youtube_pubsub_renew_job

async def main():
    print("Starting YouTube PubSubHubbub renew job manually...")
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
    await _run_youtube_pubsub_renew_job()
    print("Finished YouTube PubSubHubbub renew job.")

if __name__ == "__main__":
    asyncio.run(main())
