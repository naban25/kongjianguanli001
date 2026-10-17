# Production Dockerfile for FastAPI backend
# Build: docker build -t office-lease-api .
# Run:   docker run -p 8000:8000 --env-file .env.prod office-lease-api

FROM python:3.12-slim
WORKDIR /app

# System deps for psycopg + openpyxl
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY apps/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source (alembic is inside apps/api/)
COPY apps/api/ ./apps/api/
COPY alembic.ini ./

ENV PYTHONPATH=/app/apps/api
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000

# Run migrations from /app (where alembic.ini lives), then gunicorn from apps/api
CMD ["sh", "-c", "cd /app && \
    python -m alembic upgrade head 2>&1 && \
    cd /app/apps/api && \
    gunicorn app.main:app \
      --workers 4 \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:${PORT} \
      --access-logfile - \
      --error-logfile - \
      --timeout 310 \
      --graceful-timeout 320 \
      --keep-alive 5 \
      --max-requests 1000 \
      --max-requests-jitter 100"]
