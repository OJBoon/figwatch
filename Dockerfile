FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first — layer rebuilds when pyproject.toml changes
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[server]"

# Then copy source — changes here don't re-trigger pip install
COPY figwatch/ ./figwatch/
COPY server.py .
RUN pip install --no-cache-dir --no-deps .

VOLUME ["/app/custom-skills"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "server.py"]
