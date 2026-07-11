FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects $PORT (defaults to 8080). Bind to it.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
