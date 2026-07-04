import asyncio
from app.main import _run_youtube_pubsub_renew_job

async def main():
    print("Starting YouTube PubSubHubbub renew job manually...")
    await _run_youtube_pubsub_renew_job()
    print("Finished YouTube PubSubHubbub renew job.")

if __name__ == "__main__":
    asyncio.run(main())
