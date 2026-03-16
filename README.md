# Percona Developer Knowledge (percona-dk)

> **Status:** Fully functional, ~7,000 doc chunks indexed, MCP + REST API working. With community interest, this could grow into an official Percona developer resource.

Semantic search and retrieval of Percona documentation for AI assistants and developer tools.

**percona-dk** ingests official Percona documentation from source (GitHub repos), chunks and embeds it locally, and exposes it via REST API and [MCP](https://modelcontextprotocol.io/) server. Your AI tools get accurate, up-to-date Percona docs — no stale training data, no fragile web scraping.

## Supported tools

percona-dk works with any AI tool that supports MCP or HTTP APIs:

| Tool | How it connects |
|------|----------------|
| **Claude Desktop** | MCP server (stdio) — add to `claude_desktop_config.json` |
| **Claude Code** | MCP server (stdio) — add to `.claude/settings.json` |
| **Cursor** | MCP server (stdio) — add to Cursor MCP settings |
| **Windsurf** | MCP server (stdio) — add to Windsurf MCP settings |
| **Open WebUI** | REST API — point to `http://localhost:8000` |
| **Any MCP client** | MCP server (stdio) |
| **Any HTTP client** | REST API on port 8000 |

## What it does

```
Percona doc repos (GitHub)
        │
        ▼
  ┌─────────────┐
  │  Ingestion   │  Clone repos → parse Markdown → chunk by heading → embed locally
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  ChromaDB    │  Local vector store (all-MiniLM-L6-v2 embeddings)
  └──────┬──────┘
         │
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌───────┐
│  API  │ │  MCP  │
│Server │ │Server │
└───────┘ └───────┘
```

- **Ingestion pipeline** — clones Percona doc repos, parses ~7,000 Markdown sections, embeds locally (no API keys needed)
- **REST API** — `POST /search`, `GET /document/{repo}/{path}`, `GET /health`, `GET /stats`
- **MCP server** — `search_percona_docs` and `get_percona_doc` tools for any MCP-compatible client

## Covered products

| Repo | Product |
|------|---------|
| `percona/psmysql-docs` | Percona Server for MySQL |
| `percona/pxc-docs` | Percona XtraDB Cluster |
| `percona/pxb-docs` | Percona XtraBackup |
| `percona/pmm-doc` | Percona Monitoring and Management |
| `percona/k8sps-docs` | Percona Operator for MySQL (PS) |
| `percona/k8spxc-docs` | Percona Operator for MySQL (PXC) |
| `percona/percona-valkey-doc` | Percona Distribution for Valkey |

### Customizing the repo list

Edit the `REPOS` line in your `.env` file to add or remove doc repos:

```bash
# Add a repo (e.g., Percona Toolkit docs):
REPOS=percona/psmysql-docs,percona/pxc-docs,...,percona/percona-toolkit-docs

# Or ingest just one repo for faster testing:
REPOS=percona/pmm-doc
```

Any public `percona/*` GitHub repo with Markdown docs will work. After editing, run `percona-dk-ingest` to rebuild the index.

## Quick start

### 1. Install

```bash
git clone https://github.com/Percona-Lab/percona-dk.git
cd percona-dk
python -m venv .venv && source .venv/bin/activate
cp .env.example .env
pip install .
```

### 2. Ingest docs (~10 minutes, one-time)

```bash
percona-dk-ingest
```

This clones all doc repos and builds a local vector database. No API keys required — embeddings run locally.

### 3. Use it

**Option A: MCP server (recommended)**

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "percona-dk": {
      "command": "/path/to/percona-dk/.venv/bin/percona-dk-mcp"
    }
  }
}
```

For Claude Code, add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "percona-dk": {
      "command": "/path/to/percona-dk/.venv/bin/percona-dk-mcp"
    }
  }
}
```

For Cursor, add via Settings → MCP Servers with the same command.

Then ask your AI tool anything about Percona products — it will automatically search the docs.

**Option B: REST API**

```bash
percona-dk-server
# Open http://localhost:8000/docs for Swagger UI
```

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "How to configure PMM for MySQL monitoring", "top_k": 5}'
```

## Keeping docs up to date

The MCP server **automatically refreshes** docs in the background. On each startup, it checks when the last ingestion ran. If it's been more than 7 days (configurable), it pulls the latest docs and re-embeds — all in the background so the server starts immediately. Existing data stays searchable during the refresh.

Configure the refresh interval in `.env`:

```bash
REFRESH_DAYS=7   # check every 7 days (default)
REFRESH_DAYS=1   # check daily
REFRESH_DAYS=0   # disable auto-refresh
```

You can also refresh manually at any time:

```bash
percona-dk-ingest
```

## Docker

```bash
docker compose build
docker compose run --rm ingest   # one-time ingestion
docker compose up -d api          # start API server
```

## How it works

1. **Ingestion** (`percona-dk-ingest`): Shallow-clones each doc repo, walks all `.md` files, splits them at h2/h3 heading boundaries into chunks of ~500-800 tokens each. Metadata includes source repo, file path, heading hierarchy, and a constructed `docs.percona.com` URL.

2. **Embedding**: ChromaDB's built-in `all-MiniLM-L6-v2` model generates 384-dimensional embeddings locally. No external API calls.

3. **Search**: Queries are embedded with the same model and matched against the corpus using cosine similarity. Results include the original Markdown text, source metadata, and relevance scores.

## Project structure

```
percona-dk/
├── src/percona_dk/
│   ├── ingest.py       # Ingestion pipeline
│   ├── server.py       # FastAPI REST server
│   ├── mcp_server.py   # MCP server for AI tools
│   └── version_check.py # Update notifications
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Future direction

Potential next steps:

- **Better embeddings** — swap in a larger model for improved search quality
- **Incremental re-ingestion** — only re-embed changed files (based on git diff)
- **Version-aware search** — filter results by product version (8.0 vs 8.4)
- **Expanded corpus** — PostgreSQL docs, blog posts, knowledge base articles
- **Hosted service** — centrally hosted API for team-wide or customer access

## License

Apache 2.0
