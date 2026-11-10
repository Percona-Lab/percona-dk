# Deploying percona-dk behind the VPN

This is the runbook for hosting percona-dk (Percona Developer Knowledge) on an
IT-managed VM. IT owns the VM, the reverse proxy, HTTPS, the path allowlist and
`/health` monitoring. This repo provides the container image, a compose file and
the settings below.

## What runs

One image, three roles, chosen by command. All three share one data volume.

| Service  | Command | Port | What it does |
|----------|---------|------|--------------|
| `api`    | `python -m percona_dk.server` | 8000 | REST API (search, document fetch, feedback) |
| `mcp`    | `python -m percona_dk.mcp_server --transport streamable-http --port 8402` | 8402 | MCP server for AI tools, served at `/mcp` |
| `ingest` | `python -m percona_dk.ingest` | none | One-shot job: pulls docs, blog and forum content and updates the index. Run daily. |

`api` and `mcp` are long-running. `ingest` runs, exits, and is triggered on a
schedule (see [Daily ingest](#daily-ingest)).

## Image

```
ghcr.io/percona-lab/percona-dk:latest
ghcr.io/percona-lab/percona-dk:sha-<7-char git sha>
```

GitHub Actions (`.github/workflows/docker-publish.yml`) builds and pushes both
tags on every push to `main`. For production, pinning a `sha-` tag is
recommended so updates are deliberate:

```bash
# in .env next to docker-compose.yml
PERCONA_DK_IMAGE=ghcr.io/percona-lab/percona-dk:sha-abc1234
```

The image is `linux/amd64`, based on `python:3.12-slim`, and runs as a non-root
user `app` (UID/GID 10001). Chroma's embedding model (all-MiniLM-L6-v2, ONNX)
is baked in at build time, so containers never download it at runtime.

If `docker pull` returns `unauthorized`, the GHCR package is still private. Ask
the percona-dk maintainers to make it public, or `docker login ghcr.io` with a
GitHub token that has `read:packages`.

## Host requirements

- Docker Engine with the Compose v2 plugin, version 2.24 or later (the compose
  file marks `.env` as optional).
- Disk: the full data volume is about 2 GB today (about 0.8 GB index, 1.2 GB
  repo clones) and grows slowly. Allow at least 10 GB for the volume plus images.
- Memory: `api` and `mcp` each load the full index and the embedding model into
  memory. Size the VM for two copies of the index plus an ingest run.

## Files on the VM

Only two files are needed, in one directory (for example `/opt/percona-dk`):

- `docker-compose.yml` from this repo
- `.env` (optional, but needed for the production repo list, see below)

## Environment variables

Settings are read from the environment. Compose loads them from `.env` in the
same directory as `docker-compose.yml` and passes them to all three services.
There are **no secrets** for now: nothing below is a credential, and no
endpoint requires authentication. Access control is the VPN plus the
reverse-proxy allowlist.

### Application settings

| Variable | Default | Used by | Notes |
|----------|---------|---------|-------|
| `DATA_DIR` | `/data` | all | Where the index, repo clones and feedback live. Set to `/data` by the image and compose; leave it alone and mount the volume there. |
| `REPOS` | `percona/psmysql-docs` | ingest, api, mcp | Comma-separated GitHub doc repos to index, each `owner/repo` or `owner/repo:branch` (one entry per indexed version). **Set this**: the default indexes one repo only. The production list is below. |
| `INGEST_BLOG` | `true` | ingest | Index the Percona Community blog (percona.community). |
| `INGEST_FORUM` | `true` | ingest | Index the Percona forums (forums.percona.com). The first forum ingest into an empty volume takes several hours; later runs are incremental. |
| `REFRESH_DAYS` | `7` | mcp | MCP-server auto-refresh on startup. Compose forces `0` (disabled) for `mcp`, because `ingest` runs on its own schedule. |
| `BLOG_BASE_URL` | `https://percona.community` | ingest | Override only to test against a mirror. |
| `FORUM_BASE_URL` | `https://forums.percona.com` | ingest | Override only to test against a mirror. |
| `COMMUNITY_REQUEST_DELAY` | `0.25` | ingest | Seconds between requests to the blog and forum sites. `.env.example` uses `1.0`, which is gentler on the forum. |
| `FORUM_UPSERT_BATCH` | `500` | ingest | Forum chunks per index write. No need to change. |
| `ANONYMIZED_TELEMETRY` | `False` | all | Set in the image so Chroma does not send telemetry. |

### Compose settings

| Variable | Default | Notes |
|----------|---------|-------|
| `PERCONA_DK_IMAGE` | `ghcr.io/percona-lab/percona-dk:latest` | Image (and tag) for all three services. |
| `API_PORT` | `8000` | Host port for the REST API. Use `127.0.0.1:8000` to publish on loopback only when the reverse proxy runs on the same VM. |
| `MCP_PORT` | `8402` | Host port for the MCP server. Same loopback option: `127.0.0.1:8402`. |

### Production `.env`

This matches the repo list on the current shared instance:

```bash
REPOS=percona/psmysql-docs:8.0,percona/psmysql-docs:8.4,percona/pxc-docs:8.0,percona/pxc-docs:8.4,percona/pxb-docs:8.0,percona/pxb-docs:8.4,percona/pdmysql-docs:8.0,percona/pdmysql-docs:8.4,percona/psmdb-docs:6.0,percona/psmdb-docs:7.0,percona/psmdb-docs:8.0,percona/postgresql-docs:16,percona/postgresql-docs:17,percona/pbm-docs,percona/pmm-doc,percona/k8sps-docs,percona/k8spxc-docs,percona/k8spsmdb-docs,percona/k8spg-docs,percona/ps-binlog-server-docs,percona/percona-valkey-doc,percona/pg_tde,percona/pgsm-docs,percona/pcsm-docs,percona/percona-toolkit,percona/pmm_dump_docs,percona/repo-config-docs,openeverest/everest-doc
INGEST_BLOG=true
INGEST_FORUM=true
COMMUNITY_REQUEST_DELAY=1.0
API_PORT=127.0.0.1:8000
MCP_PORT=127.0.0.1:8402
```

Not used on this instance: the email failure alerts, the Google Sheets usage
webhook and the `scripts/` usage reports from the old systemd setup
(`deploy.sh`). `/health` freshness replaces them.

## Ports and paths

| Service | Container port | Paths |
|---------|----------------|-------|
| `api` | 8000 | `/health`, `/search`, `/document`, `/document/...`, `/feedback` (plus `/stats`, `/docs`, `/openapi.json`, `/redoc`, which should not be public) |
| `mcp` | 8402 | `/mcp` (streamable HTTP MCP), `/health` |

### Public path allowlist (reverse proxy)

Allow only these; block everything else.

**REST API (port 8000)**

| Method | Path |
|--------|------|
| GET | `/health` |
| GET, POST | `/search` |
| GET | `/document` |
| GET | `/document/*` |
| POST | `/feedback` |

Block in particular: `/stats` (slow, loads all index metadata), `/docs`,
`/openapi.json`, `/redoc`.

**MCP server (port 8402)**

| Method | Path |
|--------|------|
| GET, POST, DELETE | `/mcp` |
| GET | `/health` |

Notes for the MCP route:

- Streamable HTTP uses POST for requests, GET for the server event stream and
  DELETE to end a session. Responses can be `text/event-stream`, so turn off
  response buffering for `/mcp` and allow long read timeouts (several minutes).
- Pass the `Mcp-Session-Id` request and response headers through unchanged.
- `/mcp/` (trailing slash) answers with a 307 redirect to `/mcp`. Clients
  should be given the URL without the slash.

## Volume

Compose creates a named volume, `percona-dk-data`, mounted at `/data` in all
three services. It holds:

| Path | Contents | Rebuildable? |
|------|----------|--------------|
| `chroma/` | The search index (ChromaDB) | Yes, by ingest |
| `repos/` | Git clones of the doc repos | Yes, by ingest |
| `.last_ingest` | Timestamp marker written at the end of each successful ingest | Yes |
| `.last_ingest_files.json`, `.last_ingest_community.json` | Incremental-ingest state | Yes (a full rebuild is slower) |
| `feedback.jsonl` | Agent feedback submitted via `POST /feedback` and the MCP `give_feedback` tool | **No**, back this up |

Files are owned by UID/GID 10001. If you use a host bind mount instead of the
named volume, `chown -R 10001:10001` the host directory first.

## Outbound network access

| Destination | Needed by | Why |
|-------------|-----------|-----|
| `github.com` (HTTPS, 443) | `ingest` | `git clone` / `git pull` of the public doc repos |
| `percona.community` (HTTPS) | `ingest` | Community blog content (when `INGEST_BLOG=true`) |
| `forums.percona.com` (HTTPS) | `ingest` | Forum content (when `INGEST_FORUM=true`) |
| `ghcr.io`, `pkg-containers.githubusercontent.com` | the Docker host | Pulling the image |
| `api.github.com` | all, optional | Startup check for a newer release. 3-second timeout; if blocked it is skipped silently. |

`api` and `mcp` need no other outbound access. The embedding model is in the
image, and Chroma telemetry is off.

## First start

```bash
cd /opt/percona-dk
docker compose pull
```

The servers need an index before they can answer searches. Either build one
or copy an existing one.

**Option A: build from scratch.** The doc repos take minutes; the first forum
ingest takes several hours. Run it in the background:

```bash
nohup docker compose run --rm ingest > ingest-first-run.log 2>&1 &
```

**Option B: copy the current shared instance's data** (faster). Copy the
whole data directory from the old host (`~/percona-dk/data`, about 2 GB) to
the new VM, then load it into the volume:

```bash
docker compose run --rm --user root -v /path/to/copied/data:/src:ro ingest \
  sh -c 'cp -a /src/. /data/ && chown -R 10001:10001 /data'
```

This uses the `ingest` service only to reach the compose volume; it copies
and exits without ingesting. Then run one `docker compose run --rm ingest` to
bring the copy current.

Start the servers:

```bash
docker compose up -d
docker compose ps        # both should become "healthy"
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8402/health
```

## Daily ingest

Run the ingest job every day at 03:00 UTC, then restart `api` and `mcp`. The
servers cache the index in memory, so they only see the new content after a
restart (a later release will swap in the new index without a restart, and the
restart step can then be dropped). If ingest fails, the restart is skipped and
the servers keep serving the previous index; `/health` then shows a growing
`index_age_hours` (see [Monitoring](#monitoring)).

**Option 1: host cron** (`/etc/cron.d/percona-dk`):

```cron
CRON_TZ=UTC
0 3 * * * root cd /opt/percona-dk && docker compose run --rm ingest >> /var/log/percona-dk-ingest.log 2>&1 && docker compose restart api mcp >> /var/log/percona-dk-ingest.log 2>&1
```

If your cron does not support `CRON_TZ`, make sure the host clock is UTC or
adjust the hour.

**Option 2: systemd timer**

`/etc/systemd/system/percona-dk-ingest.service`:

```ini
[Unit]
Description=percona-dk daily ingest
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/percona-dk
ExecStart=/usr/bin/docker compose run --rm ingest
ExecStartPost=/usr/bin/docker compose restart api mcp
```

`/etc/systemd/system/percona-dk-ingest.timer`:

```ini
[Unit]
Description=Run percona-dk ingest daily at 03:00 UTC

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now percona-dk-ingest.timer
systemctl start percona-dk-ingest.service   # run once now to test
journalctl -u percona-dk-ingest.service      # ingest output
```

`ExecStartPost` only runs if ingest succeeds.

## Monitoring

Both servers answer `GET /health` with JSON:

```json
{
  "status": "ok",
  "doc_count": 87150,
  "uptime_seconds": 39834.8,
  "last_ingest": "2026-11-10T03:41:12+00:00",
  "index_age_hours": 9.3
}
```

| Field | Meaning |
|-------|---------|
| `status` | `ok`; the MCP server reports `degraded` if it cannot open the index |
| `doc_count` | Chunks in the index. `0` means no index yet or it failed to load |
| `uptime_seconds` | Seconds since this server process started |
| `last_ingest` | UTC ISO 8601 time the last successful ingest finished (`null` if none has) |
| `index_age_hours` | Hours since `last_ingest` (`null` if none has) |

Suggested alerts:

- `/health` on either port is not HTTP 200, or `status` is not `ok`
- `doc_count` is `0`
- `index_age_hours` is above 36 (the daily 03:00 job has missed a run), or
  `last_ingest` is `null` after the first ingest

The compose health checks also call `/health` (every 30 s, 3-minute start
period while the index loads), so `docker compose ps` shows `healthy` /
`unhealthy`.

## Logs

```bash
docker compose logs -f api mcp
```

Logs go to Docker's `json-file` driver, rotated at 20 MB x 5 files per
container. The MCP server logs one `MCP headers: {...}` line per `/mcp`
request (used to see which clients connect). Authorization, Cookie,
Set-Cookie, Proxy-Authorization and any header with `key` or `token` in its
name are replaced with `[REDACTED]` before logging.

## Updating

```bash
cd /opt/percona-dk
docker compose pull          # or change PERCONA_DK_IMAGE to a new sha- tag first
docker compose up -d
```

The data volume is kept across updates.

## Pointing clients at the new instance

- MCP clients: `https://<new-host>/mcp` (or whatever path the proxy maps to
  port 8402's `/mcp`).
- The remote shim (`percona-dk-mcp-remote`) talks to the REST API. Set
  `PERCONA_DK_BACKEND` to the API base URL; see "Pointing the remote shim at
  another instance" in the README.
