# Channel Content Manager

Ứng dụng web quản lý nội dung kênh **YouTube Shorts** và **TikTok**: thêm kênh, quét danh sách video trong nền, xem dashboard và đánh dấu trạng thái video qua API.

## Yêu cầu hệ thống

- **Python** 3.10 trở lên (khuyến nghị 3.11+)
- **Docker** và **Docker Compose** (để chạy PostgreSQL)
- Trình duyệt Chromium cho **Playwright** (cài qua lệnh của Playwright)

## Cài đặt

### 1. Clone và vào thư mục dự án

```bash
cd manager_youtube_channel
```

### 2. Tạo môi trường ảo (khuyến nghị)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Khởi động PostgreSQL

```bash
docker compose up -d
```

Mặc định container lắng nghe cổng **5435** (xem `docker-compose.yml`).

### 4. Cấu hình biến môi trường (tùy chọn)

Mặc định chuỗi kết nối trong `app/database.py` là:

`postgresql+asyncpg://postgres:password@localhost:5435/postgres`

Nếu bạn đổi user/mật khẩu/cổng DB, đặt biến:

```bash
export DATABASE_URL="postgresql+asyncpg://USER:PASS@HOST:PORT/DBNAME"
```

**TikTok:** đặt file cookie JSON ở gốc dự án tên `cookie.json`, hoặc chỉ đường dẫn:

```bash
export TIKTOK_COOKIES_FILE="/đường/dẫn/tới/cookie.json"
```

Định dạng cookie: JSON xuất từ extension kiểu “Get cookies.txt” (Chrome); ứng dụng tự chuyển sang định dạng Netscape cho `yt-dlp`.

Giới hạn số mục playlist TikTok (mặc định 500):

```bash
export TIKTOK_YTDLP_PLAYLIST_END="500"
```

### 5. Cài dependency Python

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 6. Migration cơ sở dữ liệu

Chạy từ **thư mục gốc** dự án (nơi có `alembic.ini`):

```bash
alembic upgrade head
```

### 7. Chạy server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Hoặc dùng script tự động (Docker DB + pip + Playwright + alembic + uvicorn):

```bash
chmod +x run.sh
./run.sh
```

Mở trình duyệt: [http://localhost:8000](http://localhost:8000)

## Hướng dẫn sử dụng

### Giao diện web (Dashboard)

- Trang chủ `/`: danh sách video (phân trang), lọc theo kênh.
- Thêm kênh và kích hoạt quét thường qua API (xem bên dưới); sau khi quét xong, video sẽ hiển thị trên dashboard.

### Thêm kênh và quét

- **YouTube:** URL kênh hoặc trang Shorts (ứng dụng sẽ ưu tiên quét `/shorts`).
- **TikTok:** URL profile kênh; nên có `cookie.json` hợp lệ để `yt-dlp` lấy danh sách ổn định.

`POST /api/channels` với body JSON:

```json
{ "url": "https://www.youtube.com/@TenKenh/shorts" }
```

hoặc

```json
{ "url": "https://www.tiktok.com/@username" }
```

Phản hồi gồm `channel_id` — dùng để theo dõi trạng thái quét.

### Theo dõi trạng thái quét

`GET /api/channels/{channel_id}/status`

Yêu cầu đăng nhập. Nếu chưa xác thực sẽ trả `401`.

Trả về `scraping_status`, `last_scraped_at`, `scraping_error` (nếu lỗi).

### TikTok Daily Sync (Scraping Manager)

- Hệ thống tự chạy lúc `00:00` mỗi ngày theo múi giờ `Asia/Ho_Chi_Minh`.
- Nguồn quét: toàn bộ kênh TikTok trong bảng `channels` (tab Scraping Manager).
- Video đã tồn tại trong `video_links` sẽ được bỏ qua.
- Video mới sẽ được thêm tự động vào `video_links`.

### Quét lại kênh

`POST /api/channels/{channel_id}/scrape`

Yêu cầu đăng nhập. Nếu chưa xác thực sẽ trả `401`.

Trả `409` nếu quét đang chạy cho kênh đó.

### API video

| Phương thức | Đường dẫn | Mô tả |
|-------------|-----------|--------|
| `GET` | `/api/videos` | Danh sách video trạng thái “available”; query `channel_link` (URL kênh) để lọc |
| `PATCH` | `/api/videos/{video_id}` | Cập nhật `status` (body: `{"status": "..."}` theo enum trong code) |

Tài liệu tương tác: [http://localhost:8000/docs](http://localhost:8000/docs) (Swagger UI của FastAPI).

## Ghi chú kỹ thuật

- **Khởi tạo bảng:** ứng dụng gọi `create_all` khi startup; vẫn nên chạy Alembic để đồng bộ schema theo migration.
- **YouTube:** dùng Playwright (headless Chromium); nếu timeout có thể tạo file `debug_youtube.png` trong thư mục làm việc hiện tại khi chạy.
- **Log:** có thể ghi `uvicorn` ra file log nếu bạn redirect output — không commit file log vào git.

## Dừng dịch vụ

```bash
docker compose down
```

Dữ liệu Postgres trong volume `postgres_data` được giữ lại trừ khi bạn xóa volume.

## Giấy phép

Thêm thông tin license của dự án tại đây nếu có.
