FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NIGHTSHIFT_HOME=/data \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-liberation tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY nightshift ./nightshift
RUN pip install . && python -m playwright install --with-deps chromium

VOLUME /data
EXPOSE 8080
CMD ["nightshift", "server"]
