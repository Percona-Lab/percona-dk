# Percona Developer Knowledge (percona-dk)

> **Status:** Fully functional, 22 doc repos across 7 stacks, MCP + REST API working. Supports Markdown and reStructuredText. With community interest, this could grow into an official Percona developer resource.

Semantic search and retrieval of Percona documentation for AI assistants and developer tools.

**percona-dk** ingests official Percona documentation from source (GitHub repos), chunks and embeds it locally, and exposes it via REST API and [MCP](https://modelcontextprotocol.io/) server. Your AI tools get accurate, up-to-date Percona docs -- no stale training data, no fragile web scraping.

## Why this matters

It's not just about new information. Percona DK helps in three distinct ways:

1. **New features the LLM can't know about** -- PXC 8.4 added a Clone plugin for SST in April 2025. No LLM has this in training data. Without DK, the AI confidently tells you the feature doesn't exist.

2. **Percona-specific products the LLM overlooks** -- Percona built a dedicated tool for Atlas-to-PSMDB migrations (Percona Link for MongoDB). Without DK, the AI recommends `mongosync` or a DIY approach. The right tool exists -- the LLM just doesn't know about it.

3. **Operational details the LLM gets vaguely right but not precisely right** -- This is the most common day-to-day value. The AI gives you a reasonable answer, but DK gives you the exact flags, version constraints, setup gotchas (like needing to enable MongoDB profiling for PMM Query Analytics), and copy-paste commands from current docs. When you're writing production configs or answering a customer, "mostly right" isn't good enough.

## Supported tools

percona-dk works with any AI tool that supports MCP or HTTP APIs:

| Tool | How it connects | Windows |
| --- | --- | --- |
| **Claude Desktop** | MCP server (stdio) - add to `claude_desktop_config.json` | Yes |
| **Claude Code** | MCP server (stdio) - add to `.claude/settings.json` | Yes |
| **Cursor** | MCP server (stdio) - add to `.cursor/mcp.json` | Yes |
| **Windsurf** | MCP server (stdio) - add to Windsurf MCP settings | Yes |
| **GitHub Copilot** | MCP server (stdio) - add to `.vscode/mcp.json`, use Agent Mode | Yes |
| **OpenAI Codex CLI** | MCP server (stdio) - add to `~/.codex/config.toml` | WSL only |
| **Codex IDE extension** | MCP server (stdio) - shares config with Codex CLI | Yes (VS Code) |
| **Cherry Studio** | MCP server (stdio) - add to MCP settings | Yes |
| **LM Studio** | MCP server (stdio) - configure in MCP client settings | Yes |
| **AnythingLLM** | MCP server (stdio) - edit `anythingllm_mcp_servers.json` | Yes |
| **Open WebUI** | REST API - point to `http://localhost:8000` | Yes |
| **LibreChat** | REST API or MCP via proxy - configure in YAML | Yes |
| **Any MCP client** | MCP server (stdio) | - |
| **Any HTTP client** | REST API on port 8000 | - |

> **Windows note:** percona-dk itself runs on Windows natively (Python + pip install). For the Codex CLI specifically, OpenAI recommends running inside WSL, though the Codex IDE extension in VS Code works natively. All other tools listed above work on Windows without WSL.

> **LLM compatibility:** MCP is a protocol, not a model feature. Any LLM with tool/function-calling support works, including Claude, GPT-4o, Gemini, Qwen, Llama (via Ollama), Mistral, and others. Reasoning-only models without tool-calling support are not compatible.

## Quick start

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/install-percona-dk | bash
```

**Windows** (PowerShell):
```powershell
irm https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/install-percona-dk.ps1 | iex
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
  │  Ingestion   │  Clone repos → parse Markdown/RST → chunk by heading → embed locally
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

- **Ingestion pipeline** — clones Percona doc repos, parses Markdown and reStructuredText sections, embeds locally (no API keys needed)
- **REST API** — `POST /search`, `GET /document/{repo}/{path}`, `GET /health`, `GET /stats`
- **MCP server** — `search_percona_docs` and `get_percona_doc` tools for any MCP-compatible client

## Available repos

The installer lets you choose which stacks to index. All repos are public Percona GitHub repositories.

| Stack | Repo | Product |
|-------|------|---------|
| **MySQL** | `percona/psmysql-docs` | Percona Server for MySQL |
| **MySQL** | `percona/pxc-docs` | Percona XtraDB Cluster |
| **MySQL** | `percona/pxb-docs` | Percona XtraBackup |
| **MySQL** | `percona/pdmysql-docs` | Percona Distribution for MySQL |
| **MySQL** | `percona/ps-binlog-server-docs` | Percona Binlog Server |
| **MongoDB** | `percona/psmdb-docs` | Percona Server for MongoDB |
| **MongoDB** | `percona/pbm-docs` | Percona Backup for MongoDB |
| **MongoDB** | `percona/pcsm-docs` | Percona ClusterSync for MongoDB |
| **PostgreSQL** | `percona/postgresql-docs` | Percona Distribution for PostgreSQL |
| **PostgreSQL** | `percona/pg_tde` | pg_tde (Transparent Data Encryption) |
| **PostgreSQL** | `percona/pgsm-docs` | pg_stat_monitor |
| **Valkey** | `percona/percona-valkey-doc` | Percona Packages for Valkey |
| **Kubernetes Operators** | `percona/k8sps-docs` | Operator for MySQL |
| **Kubernetes Operators** | `percona/k8spxc-docs` | Operator for PXC |
| **Kubernetes Operators** | `percona/k8spsmdb-docs` | Operator for MongoDB |
| **Kubernetes Operators** | `percona/k8spg-docs` | Operator for PostgreSQL |
| **OpenEverest** | `openeverest/everest-doc` | OpenEverest DBaaS Platform |
| **Tools and PMM** | `percona/pmm-doc` | Percona Monitoring and Management |
| **Tools and PMM** | `percona/pmm_dump_docs` | PMM Dump |
| **Tools and PMM** | `percona/proxysql-admin-tool-doc` | ProxySQL Admin Tool |
| **Tools and PMM** | `percona/percona-toolkit` | Percona Toolkit (RST docs) |
| **Tools and PMM** | `percona/repo-config-docs` | Percona Software Repositories |

The MySQL stack and Tools are indexed by default. MongoDB, PostgreSQL, Kubernetes Operators, and OpenEverest are opt-in during installation.

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

For GitHub Copilot (VS Code), add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "percona-dk": {
      "command": "/path/to/percona-dk/.venv/bin/percona-dk-mcp"
    }
  }
}
```

Then switch to **Agent Mode** in Copilot Chat to use MCP tools.

For OpenAI Codex CLI, add to `~/.codex/config.toml`:

```toml
[mcp_servers.percona-dk]
command = ["/path/to/percona-dk/.venv/bin/percona-dk-mcp"]
```

## Keeping docs up to date

The MCP server **automatically syncs** docs in the background. On each startup, it checks when the last sync ran. If it's been more than 7 days (configurable), it pulls the latest from GitHub and re-embeds only the files that changed — all in the background so the server starts immediately. Existing data stays searchable during the sync.

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

1. **Ingestion** (`percona-dk-ingest`): Shallow-clones each doc repo, walks all `.md` and `.rst` files, splits them at h2/h3 heading boundaries into chunks of ~500-800 tokens each. Metadata includes source repo, file path, heading hierarchy, and a constructed `docs.percona.com` URL.

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
- **Version-aware search** — filter results by product version (8.0 vs 8.4)
- **Expanded corpus** — blog posts, knowledge base articles
- **Hosted service** — centrally hosted API for team-wide or customer access

## License

Apache 2.0
