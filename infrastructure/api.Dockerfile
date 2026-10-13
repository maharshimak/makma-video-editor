# API and worker image. Same image, different command -- the only difference
# between serving requests and rendering video is which process starts.
FROM python:3.12-slim

# FFmpeg is the render engine, not an optional extra.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MAKMA_DATA_DIR=/data

WORKDIR /app

COPY apps/api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/api/makma ./makma

RUN useradd --create-home --uid 10001 makma \
    && mkdir -p /data \
    && chown -R makma:makma /data /app
USER makma

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/system/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "makma.main:app", "--host", "0.0.0.0", "--port", "8000"]
