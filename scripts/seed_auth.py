"""
Tạo user mặc định để đăng nhập (chạy sau khi migration / init DB).

Mặc định:
  SEED_USERNAME=admin
  SEED_PASSWORD=admin123

Chạy từ thư mục gốc dự án:
  python scripts/seed_auth.py
"""
import asyncio
import os
import sys

# Thư mục gốc dự án trên sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import select  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402


async def main() -> None:
    await init_db()
    username = os.getenv("SEED_USERNAME", "admin")
    password = os.getenv("SEED_PASSWORD", "admin123")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            print(f"User '{username}' đã tồn tại, bỏ qua.")
            return

        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        await db.commit()
        print(f"Đã tạo user: {username} (đặt SEED_PASSWORD để đổi mật khẩu khi seed lần đầu).")


if __name__ == "__main__":
    asyncio.run(main())
