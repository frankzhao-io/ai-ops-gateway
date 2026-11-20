FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY dashboard/ ./dashboard/

ENV PYTHONUNBUFFERED=1 \
    AI_OPS_LIVE=0 \
    AI_OPS_MAX_LOCAL_CONCURRENCY=2

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "src.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
