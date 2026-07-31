FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data

ENV MAILROOM_DB_PATH=/app/data/mailroom.db
ENV MAILROOM_HMAC_SECRET_PATH=/app/data/hmac_secret

EXPOSE 8000

# Single worker: our SQLite + in-process asyncio.Lock durability model
# assumes one process. If you need more throughput, swap SQLite for
# Postgres in db.py and increase workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
