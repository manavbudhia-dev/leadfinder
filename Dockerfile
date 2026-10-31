FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries=10 -r requirements.txt

COPY app.py lead_pipeline.py settings_store.py jobs.py throttle.py backup.py linkedin.py outreach.py sheets.py ./
COPY static ./static

RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME /data
EXPOSE 8000

# ONE worker + threads: DB_LOCK aur SQLite single-writer design ke liye zaruri.
# Timeout 300s: Reoon power-mode verify slow hota hai, batch lamba chal sakta hai.
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 300 -b 0.0.0.0:${PORT:-8000} app:app"]