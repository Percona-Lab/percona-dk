# Percona Developer Knowledge (percona-dk)

> **Status:** Fully functional, ~7,000 doc chunks indexed, MCP + REST API working. With community interest, this could grow into an official Percona developer resource.

Semantic search and retrieval of Percona documentation for AI assistants and developer tools.

**percona-dk** ingests official Percona documentation from source (GitHub repos), chunks and embeds it locally, and exposes it via REST API and [MCP](https://modelcontextprotocol.io/) server. Your AI tools get accurate, up-to-date Percona docs — no stale training data, no fragile web scraping.

## Supported tools

percona-dk works with any AI tool that supports MCP or HTTP APIs:

| Tool | How it connects |
|------|----------------|
| **Claude Desktop** | MCP server (stdio) — auto-configured by installer |
| **Claude Code** | MCP server (stdio) — auto-configured by installer |
| **Cursor** | MCP server (stdio) — add to Cursor MCP settings |
| **Windsurf** | MCP server (stdio) — add to Windsurf MCP settings |
| **Zed** | MCP server (stdio) — add to Zed MCP settings |
| **Open WebUI** | REST API — point to `http://localhost:8000` |
| **Any MCP client** | MCP server (stdio) |
| **Any HTTP client** | REST API on port 8000 |

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/install-percona-dk | bash
```

The installer handles everything:

- Installs `uv` if needed (downloads Python 3.12 automatically — no system Python required)
- Clones the repo to `~/percona-dk`
- Creates an isolated virtual environment
- Walks you through selecting which doc repos to index (grouped by product stack, with live size estimates)
- Asks how often to auto-sync (default: every 7 days)
- Auto-configures Claude Desktop and Claude Code
- Runs the initial ingestion

Safe to re-run — detects existing installs, preserves your config, and pre-selects repos you already have indexed.

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

- **Ingestion pipeline** — clones Percona doc repos, parses Markdown sections, embeds locally (no API keys needed)
- **REST API** — `POST /search`, `GET /document/{repo}/{path}`, `GET /health`, `GET /stats`
- **MCP server** — `search_percona_docs` and `get_percona_doc` tools for any MCP-compatible client

## Available repos

The installer lets you choose which stacks to index. All repos are public Percona GitHub repositories.

| Stack | Repo | Product |
|-------|------|---------|
| **MySQL** | `percona/psmysql-docs` | Percona Server for MySQL |
| **MySQL** | `percona/pxc-docs` | Percona XtraDB Cluster |
| **MySQL** | `percona/pxb-docs` | Percona XtraBackup |
| **MySQL** | `percona/pmm-doc` | Percona Monitoring and Management |
| **MongoDB** | `percona/psmdb-docs` | Percona Server for MongoDB |
| **MongoDB** | `percona/pbm-docs` | Percona Backup for MongoDB |
| **PostgreSQL** | `percona/postgresql-docs` | Percona Distribution for PostgreSQL |
| **Kubernetes Operators** | `percona/k8sps-docs` | Operator for MySQL |
| **Kubernetes Operators** | `percona/k8spxc-docs` | Operator for PXC |
| **Kubernetes Operators** | `percona/k8spsmdb-docs` | Operator for MongoDB |
| **Kubernetes Operators** | `percona/k8sppg-docs` | Operator for PostgreSQL |
| **Tools** | `percona/proxysql-admin-tool-doc` | ProxySQL Admin Tool |

The MySQL stack and Tools are indexed by default. MongoDB, PostgreSQL, and Kubernetes Operators are opt-in during installation.

### Adding repos after installation

Re-run the installer — it will show your current selection with existing repos pre-ticked, detect the change, and prompt you to re-index:

```bash
curl -fsSL https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/install-percona-dk | bash
```

Or edit `.env` directly and re-run ingestion:

```bash
# Edit ~/percona-dk/.env, then:
DOTENV_PATH=~/percona-dk/.env ~/percona-dk/.venv/bin/percona-dk-ingest
```

## Manual MCP configuration

If you need to configure an MCP client manually, use:

```json
{
  "mcpServers": {
    "percona-dk": {
      "command": "/path/to/percona-dk/.venv/bin/python",
      "args": ["-m", "percona_dk.mcp_server"],
      "env": { "DOTENV_PATH": "/path/to/percona-dk/.env" }
    }
  }
}
```

For Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `~/.config/Claude/claude_desktop_config.json` (Linux).

For Claude Code: `~/.claude/settings.json`.

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
DOTENV_PATH=~/percona-dk/.env ~/percona-dk/.venv/bin/percona-dk-ingest
```

## REST API

```bash
# Start the API server
~/percona-dk/.venv/bin/percona-dk-server
# Open http://localhost:8000/docs for Swagger UI
```

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "How to configure PMM for MySQL monitoring", "top_k": 5}'
```

## How it works

1. **Ingestion** (`percona-dk-ingest`): Shallow-clones each doc repo, walks all `.md` files, splits them at h2/h3 heading boundaries into chunks of ~500-800 tokens each. Metadata includes source repo, file path, heading hierarchy, and a constructed `docs.percona.com` URL.

2. **Embedding**: ChromaDB's built-in `all-MiniLM-L6-v2` model generates 384-dimensional embeddings locally. No external API calls.

3. **Search**: Queries are embedded with the same model and matched against the corpus using cosine similarity. Results include the original Markdown text, source metadata, and relevance scores.

4. **Repo suggestions**: If a search returns weak results and the query matches keywords from a repo that isn't indexed, the MCP server suggests adding that repo.

## Project structure

```
percona-dk/
├── src/percona_dk/
│   ├── ingest.py          # Ingestion pipeline
│   ├── server.py          # FastAPI REST server
│   ├── mcp_server.py      # MCP server for AI tools
│   ├── repo_registry.py   # Known repos + suggestion logic
│   └── version_check.py   # Update notifications
├── install-percona-dk     # One-line installer
├── pyproject.toml
└── .env.example
```

## Future direction

Potential next steps:

- **Better embeddings** — swap in a larger model for improved search quality
- **Incremental re-ingestion** — only re-embed changed files (based on git diff)
- **Version-aware search** — filter results by product version (8.0 vs 8.4)
- **Expanded corpus** — blog posts, knowledge base articles
- **Hosted service** — centrally hosted API for team-wide or customer access

## License

Apache 2.0
