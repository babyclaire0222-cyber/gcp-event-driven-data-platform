FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root
RUN useradd --create-home appuser
USER appuser

# Cloud Run sets $PORT. One process with threads: the app is I/O-bound (waits on GCP APIs), and a
# single process keeps the in-memory rate limiter exact per instance when REDIS_URL is unset.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --worker-class gthread --timeout 120 main:app
