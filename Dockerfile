# percona-dk: one image, three roles (chosen by command):
#   ingest  python -m percona_dk.ingest
#   api     python -m percona_dk.server                 (REST API, port 8000)
#   mcp     python -m percona_dk.mcp_server --transport streamable-http --port 8402  (/mcp)
#
# The index lives in DATA_DIR (/data), which should be a volume shared by
# all three roles. See DEPLOY.md.

FROM python:3.12-slim

# git: ingest clones the docs repos (GitPython shells out to it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fixed UID/GID so a host bind mount for /data can be chowned to match.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid app --create-home --home-dir /home/app app \
    && mkdir -p /data \
    && chown app:app /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    DATA_DIR=/data \
    ANONYMIZED_TELEMETRY=False

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && rm -rf /app/src /app/build

USER app

# Bake Chroma's default embedding model (all-MiniLM-L6-v2 ONNX) into the
# image. Chroma downloads it to ~/.cache/chroma/onnx_models on first use;
# doing that here, as the runtime user, means the container never needs
# to fetch it at runtime.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warmup'])" \
    && test -f /home/app/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx

VOLUME ["/data"]
EXPOSE 8000 8402

# Default role is the REST API; compose overrides the command per service.
CMD ["python", "-m", "percona_dk.server"]
