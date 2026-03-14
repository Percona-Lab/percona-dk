FROM python:3.12-slim

WORKDIR /app

# Install git for repo cloning
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY src/ src/

# Data volume for ChromaDB persistence and cloned repos
VOLUME /data

ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Expose API and MCP ports
EXPOSE 8000

# Default: run API server. Override with docker compose for MCP or ingestion.
CMD ["uvicorn", "percona_dk.server:app", "--host", "0.0.0.0", "--port", "8000"]
