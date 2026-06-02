# Percona Developer Knowledge (percona-dk)

> **Status:** Fully functional. 22 doc repos across 7 stacks, plus the Percona Community blog and the Percona forums. MCP + REST API working. Supports Markdown and reStructuredText. With community interest, this could grow into an official Percona developer resource.

**percona-dk is a ground-truth Percona lookup for AI coding agents.** When an agent — Claude Code, Cursor, Copilot, Codex, or any MCP/HTTP client — writes the install scripts, Ansible playbooks, Terraform, Kubernetes operator CRs, `my.cnf`/`mongod.conf`, Dockerfiles, or runbooks that stand up and operate Percona software, it calls percona-dk to ground each Percona-specific fact in current, version-correct, cited documentation instead of training-data memory.

It ingests three kinds of Percona knowledge — official docs from 22 GitHub repos, ~280 [percona.community](https://percona.community/blog/) blog posts, and ~16,000 [forums.percona.com](https://forums.percona.com) threads — chunks and embeds them locally, and serves them over [MCP](https://modelcontextprotocol.io/) and a REST API. Everything runs on your machine: no API keys, no scraping.

## Why an agent needs this

AI coding agents increasingly write the code that deploys and operates databases. That output is **executable and it ships to real infrastructure** — so the failure mode that matters isn't "the agent sounds unsure," it's **confident, plausible, and wrong**: a package name from the wrong repo, a flag removed two releases ago, a config key that's valid on 8.0 but gone on 8.4, an operator CR field that was renamed. It looks right. It runs. Then a node won't rejoin the cluster, or a backup turns out to be silently unrestorable.

Three things push an agent toward that failure mode, and percona-dk addresses each:

- **No web in the loop.** Most code generation happens with no web search — in an IDE, in CI, in a headless pipeline. The agent generates from training data, which has a cutoff and no Percona-specific grounding. percona-dk is a single tool call that returns the current answer, cited. **This is the case it's built for.**
- **Training data blurs versions and leans upstream.** Models absorb far more upstream MySQL/MongoDB/PostgreSQL than Percona's specific deltas, and they blur 8.0 vs 8.4 syntax. Pass `version="8.4"` and every chunk — plus the resolved `docs.percona.com/...8.4/...` URL — is scoped to the branch the agent is generating for.
- **The freshest facts postdate the model.** The corpus is re-ingested daily, so post-cutoff releases (e.g. Percona ClusterSync for MongoDB 0.8.1, the PXC Clone-plugin SST method) are present. A model with no web cannot know these at all.

**The honest boundary:** this is not pitched against a human in a browser. A person using a strong model *with* web search can usually reach the same public docs — for "how do I configure X," percona-dk ties that experience (cited inline, less drift) rather than beating it. The win is the *agent* loop: no web, executable output, version-sensitive, high cost of confidently wrong. It is largest for agents with no web access and for smaller tool-calling models that carry more stale defaults.

### What an agent gets wrong without it — each verified against the live corpus

🔵 *this model, no web, is actually wrong; percona-dk corrects it* · 🟡 *the corpus is the authoritative cite; a web chat ties*

1. **🔵 A valid method the agent denies exists.** Asked whether PXC supports the Clone plugin as an SST method, this model with no web answers *"No, PXC does not."* Wrong: the [Clone SST method](https://docs.percona.com/percona-xtradb-cluster/8.4/clone-sst/) ships in PXC 8.4.4-4 ([PXC-4469](https://perconadev.atlassian.net/browse/PXC-4469), 2025-04-16) and is a tech-preview in 8.0 (`clone` has been in the default `wsrep_sst_allowed_methods` since 8.0.41). An agent scripting SST would omit a faster, supported option.

2. **🔵 A Percona tool the agent overlooks.** Asked how to migrate MongoDB Atlas to self-hosted PSMDB, this model with no web recommends MongoDB's `mongosync` and states Percona has no dedicated tool. It does: [Percona ClusterSync for MongoDB (PCSM)](https://docs.percona.com/pcsm/latest/) — formerly Percona Link for MongoDB, current 0.8.1 — does change-stream replication for minimal-downtime migration. (Per the [forum guideline](https://forums.percona.com/t/guideline-for-migrating-from-atlas-cluster-to-percona-mongodb/36958), truly *zero*-downtime Atlas exits are constrained because Atlas doesn't expose backend nodes — plan for minimal, not zero.)

3. **🟡 Removed flags that still look current.** Training data is full of `innobackupex` (removed in XtraBackup 8.0 — use the `xtrabackup` binary), `--compress=quicklz` (removed for backup in 8.0.34-29 — the default is now ZSTD), and `innodb_track_changed_pages` (deprecated PS 8.0.27, **removed 8.0.30** — use `--page-tracking`). A strong model often knows these; a weaker one writes them into a script verbatim. The corpus is the authoritative check either way.

4. **🟡 Version-correct config in generated files.** Generating a `my.cnf` for PS 8.4, a model may reach for `innodb_log_file_size` — removed in 8.4, where `innodb_redo_log_capacity` is the only knob. `search_percona_docs(query, version="8.4")` returns only 8.4-tagged chunks, so the emitted config matches the target branch.

5. **🟡 Operator CR field reference.** Operator Custom Resource fields shift between releases; the corpus carries each operator's [CR options reference](https://docs.percona.com/k8spsmdb/latest/operator/) so an agent writing a `PerconaServerMongoDB` (or PXC/PG) manifest cites real fields instead of guessing.

**Verify in 60 seconds:** ask any of these with **web search off** — the real condition inside most coding agents — first with the connector off, then on. Watch for the removed flag, the wrong-branch config key, or the Percona-specific tool the agent never reaches for.

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

This is a **full local install** - it clones the Percona doc repos, builds a local ChromaDB index, and runs a local MCP server against it. Everything stays on your machine; it works completely offline once indexed. First-run takes minutes to hours depending on how many sources you select.

The installer handles everything:

- Installs `uv` if needed (downloads Python 3.12 automatically - no system Python required)
- Clones the repo to `~/percona-dk` and creates an isolated virtual environment
- Auto-configures Claude Desktop, Claude Code, Cursor, and Windsurf
- Walks you through which doc repos to index, runs initial ingestion, sets up auto-refresh

Safe to re-run - detects existing installs and preserves your config.

> **Want one shared instance for a team instead of a copy per machine?** Run the server once over HTTP and point every client at a URL - see [Run as a streamable HTTP MCP server (beta)](#run-as-a-streamable-http-mcp-server-beta) below.

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

## Content sources

percona-dk indexes three kinds of content into a single searchable corpus:

| Source | What it covers | Refresh |
|---|---|---|
| **Official docs** (GitHub) | 22 Percona product doc repos across 7 stacks | Incremental, daily |
| **Community blog** (percona.community/blog) | ~280 long-form posts: deep dives, tuning walkthroughs, release overviews | Daily, via sitemap `lastmod` |
| **Percona forums** (forums.percona.com) | ~16,000 Discourse topics: real-world Q&A, troubleshooting threads, configuration discussions | Daily, via sitemap `lastmod` |

Blog and forum ingestion can be toggled independently in `.env` (`INGEST_BLOG=true`, `INGEST_FORUM=true`). For existing installs, re-run `percona-dk-ingest` after pulling the latest release to pick them up.

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

## Run as a streamable HTTP MCP server (beta)

By default the MCP server runs over **stdio**, which means each AI tool spawns its own local subprocess. That's the simplest setup and what the installer wires up. If you'd rather run percona-dk **once** as a long-running HTTP service -- so multiple clients (or remote clients, or clients that prefer URL-based connectors) can all hit the same instance -- the server also supports two HTTP transports:

- **`streamable-http`** -- the current MCP HTTP transport. Recommended for new clients.
- **`sse`** -- the older Server-Sent Events transport. Still supported for clients that haven't moved to streamable-http yet.

Both are marked beta because the upstream MCP HTTP transports are still evolving and not every client implements them the same way. The stdio path is the stable default.

Start the server in HTTP mode:

```bash
# Streamable HTTP (recommended)
~/percona-dk/.venv/bin/percona-dk-mcp --transport streamable-http --host 0.0.0.0 --port 8402

# SSE (for older clients)
~/percona-dk/.venv/bin/percona-dk-mcp --transport sse --host 0.0.0.0 --port 8402
```

The endpoint is `http://your-host:8402/mcp` for streamable-http, or `http://your-host:8402/sse` for SSE.

### Where it works (and where it won't) -- read this before you pick a URL

**Which URL is reachable depends on *who* connects to it.** There are two connection models, and the difference matters:

- **Clients that connect from your own machine** -- Cursor, Windsurf, Zed, Continue, Cline, and the `mcp-remote` bridge (below). These reach whatever address *your machine* can reach: `localhost`, a LAN/private IP, a VPN address, or a public URL all work.
- **Native "add a custom connector by URL" in Claude Desktop / claude.ai** -- these do **not** connect from your machine. They proxy the MCP traffic through Anthropic's servers, so the endpoint has to be reachable **from the public internet** -- a publicly resolvable HTTPS URL (ideally with auth and TLS). A `localhost` or LAN-only address will appear to add but every tool call will fail, because Anthropic's cloud can't reach your laptop.

So:

| You're hosting on... | Cursor / Windsurf / `mcp-remote` (connect from your machine) | Claude Desktop / claude.ai native custom connector (proxied through Anthropic) |
|---|---|---|
| `localhost` / LAN / private IP | ✅ works | ❌ won't reach it |
| VPN-only address | ✅ works (if you're on the VPN) | ❌ won't reach it |
| Public HTTPS URL | ✅ works | ✅ works |

**For Claude Desktop / Claude Code against a `localhost` or LAN instance, use the `mcp-remote` bridge** -- it runs on your machine, connects locally, and exposes the server to Claude over stdio, sidestepping the public-reachability requirement entirely:

```json
{
  "mcpServers": {
    "percona-dk": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://your-host:8402/mcp"]
    }
  }
}
```

Only reach for a *native* URL custom connector when the instance is already on a public HTTPS URL.

Use cases this unlocks:

- **One shared instance for a team** -- index once on a server, let everyone's IDE hit the same corpus instead of each engineer running a local copy. On a LAN/private network, every client connects directly; expose it publicly only if you also need native Claude custom connectors.
- **Lightweight clients** -- machines that don't have Python or `uv` installed can still use percona-dk by pointing at a remote URL.
- **Custom connectors** -- any client that adds an MCP server by URL can connect, subject to the reachability rules above (direct-connect clients work on any address they can reach; native Claude connectors need a public URL).

If you just want the default single-user experience, stick with the stdio install from the Quick Start -- no flags needed.

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

- **Deeper agent affordances** -- structured/typed results, "is X supported in version Y" as a first-class answer, and empty-result guidance that tells the agent what to try next, as AI-generated install scripts, playbooks, and CRs become standard workflow
- **Better embeddings** — swap in a larger model for improved search quality
- **Source-type filtering** — let clients restrict searches to docs-only, community-only, or weight them differently
- **Additional sources** — knowledge base articles, release notes archives, conference talk transcripts
- **Hosted service** — centrally hosted API for team-wide or customer access

## License

Apache 2.0
