# Percona Developer Knowledge (percona-dk)

> **Status: Proof of Concept**
> This is an early-stage prototype. The goal is to validate the approach and gather feedback. With community interest, this could grow into an official Percona developer resource.

Semantic search and retrieval of Percona documentation for AI assistants and developer tools.

**percona-dk** ingests official Percona documentation from source (GitHub repos), chunks and embeds it locally, and exposes it via REST API and [MCP](https://modelcontextprotocol.io/) server. Your AI tools get accurate, up-to-date Percona docs — no stale training data, no fragile web scraping.

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

- **Ingestion pipeline** — clones 7 Percona doc repos, parses ~7,000 Markdown sections, embeds locally (no API keys needed)
- **REST API** — `POST /search`, `GET /document/{repo}/{path}`, `GET /health`, `GET /stats`
- **MCP server** — `search_percona_docs` and `get_percona_doc` tools for Claude Desktop, Claude Code, Cursor, or any MCP client

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

Then ask Claude anything about Percona products — it will automatically search the docs.

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

## Re-ingesting (updating docs)

Run `percona-dk-ingest` again. It pulls the latest from each repo and rebuilds the index.

## Project structure

```
percona-dk/
├── src/percona_dk/
│   ├── ingest.py       # Ingestion pipeline
│   ├── server.py       # FastAPI REST server
│   └── mcp_server.py   # MCP server for AI tools
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Future direction

This proof of concept validates that local semantic search over Percona docs is useful for AI-assisted development. Potential next steps:

- **Better embeddings** — swap in a larger model for improved search quality
- **Incremental re-ingestion** — only re-embed changed files (based on git diff)
- **Version-aware search** — filter results by product version (8.0 vs 8.4)
- **Expanded corpus** — PostgreSQL docs, blog posts, knowledge base articles
- **Hosted service** — centrally hosted API for team-wide or customer access

## License

Apache 2.0
