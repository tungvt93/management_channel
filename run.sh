#!/bin/bash

# 1. Start PostgreSQL
echo "Starting PostgreSQL via Docker..."
docker-compose up -d

# 2. Wait for DB to be ready
echo "Waiting for database..."
sleep 5

# 3. Install requirements
echo "Installing Python dependencies..."
python3 -m pip install -r requirements.txt

# 4. Install Playwright browsers
echo "Installing Playwright browsers..."
python3 -m playwright install chromium

# 5. Run database migrations
echo "Running database migrations..."
python3 -m alembic upgrade head

# 6. Run the app
echo "Starting FastAPI server..."
echo "Open http://localhost:8000 in your browser."
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
