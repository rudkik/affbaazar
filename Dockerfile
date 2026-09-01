FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./
COPY app ./app

RUN useradd -m -u 1000 botuser && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${WEB_PORT:-8080}/api/chats || exit 1

CMD ["python", "bot.py"]
