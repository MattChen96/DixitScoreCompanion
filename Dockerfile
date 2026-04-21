# Lightweight single-stage image (FastAPI + uvicorn)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

EXPOSE 8000

ENTRYPOINT ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
