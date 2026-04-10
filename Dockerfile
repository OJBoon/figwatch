FROM python:3.11-slim

# Install Node.js (required for Claude Code CLI)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install the figwatch package
COPY pyproject.toml .
COPY figwatch/ ./figwatch/
RUN pip install --no-cache-dir -e .

COPY server.py .

# Optional: mount a directory of custom skills at runtime
# docker run -v ./my-skills:/app/custom-skills figwatch
VOLUME ["/app/custom-skills"]

CMD ["python", "server.py"]
