"""
Percona DK - Remote Client Shim (stdio MCP)

A local stdio MCP server that forwards tool calls to the shared Percona DK
instance on sherpa via its REST API. Designed so the connector in Claude
Desktop / Claude Code stays registered and green even when the backend is
unreachable (off-VPN, sherpa down, etc.). Tool calls then return a
friendly "VPN required" message as a normal tool result, instead of
crashing the MCP process and leaving the user with a red "Server
disconnected" banner.

This matches the pattern used by other Percona internal MCP connectors
(Clari Copilot, Vista Data), where the MCP server runs locally but its
backing data is on VPN.

Configuration
=============
PERCONA_DK_BACKEND  Base URL of the REST API (default:
                    http://sherpa.tp.int.percona.com:8000)
PERCONA_DK_TIMEOUT  Per-request timeout in seconds (default: 15)

Install
=======
Runs via `uvx` straight from the repo, no persistent install required:

    uvx --from git+https://github.com/Percona-Lab/percona-dk percona-dk-mcp-remote

Claude Desktop config:

    {
      "mcpServers": {
        "percona-dk": {
          "command": "uvx",
          "args": [
            "--from",
            "git+https://github.com/Percona-Lab/percona-dk",
            "percona-dk-mcp-remote"
          ]
        }
      }
    }

Claude Code:

    claude mcp add percona-dk -- uvx --from \\
        git+https://github.com/Percona-Lab/percona-dk percona-dk-mcp-remote
"""

from __future__ import annotations

import logging
import os

import requests
from fastmcp import FastMCP

BACKEND_URL = os.getenv(
    "PERCONA_DK_BACKEND",
    "http://sherpa.tp.int.percona.com:8000",
).rstrip("/")

REQUEST_TIMEOUT = float(os.getenv("PERCONA_DK_TIMEOUT", "15"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


_VPN_MESSAGE = (
    "**Percona DK backend is unreachable.**\n\n"
    "This connector forwards queries to the shared Percona DK instance on "
    "the internal Percona network, which requires VPN access.\n\n"
    "**What to do:**\n"
    "1. Connect to the Percona VPN.\n"
    "2. Re-run your query.\n\n"
    f"If you are on the VPN and still see this, the shared instance at "
    f"`{BACKEND_URL}` may be down; ping in Slack or fall back to a local "
    f"install of percona-dk."
)


def _vpn_error_with_detail(detail: str) -> str:
    return f"{_VPN_MESSAGE}\n\n*Technical detail:* {detail}"


def _format_search_results(data: dict) -> str:
    """Render the REST /search JSON response as markdown that matches
    the native MCP tool's output format."""
    results = data.get("results") or []
    if not results:
        msg = "No results found for your query."
        suggestion = data.get("suggestion")
        if suggestion:
            msg += f"\n\n{suggestion}"
        return msg

    parts: list[str] = []
    for i, r in enumerate(results, 1):
        version_meta = r.get("version") or ""
        version_line = f"**Version:** {version_meta}\n" if version_meta else ""
        parts.append(
            f"### Result {i} (relevance: {r.get('score', '?')})\n"
            f"**Source:** {r.get('source_repo', '')} - `{r.get('file_path', '')}`\n"
            f"{version_line}"
            f"**Section:** {r.get('heading_hierarchy', '')}\n"
            f"**URL:** {r.get('page_url', '')}\n\n"
            f"{r.get('text', '')}\n"
        )
    output = "\n---\n".join(parts)

    suggestion = data.get("suggestion")
    if suggestion:
        output += f"\n\n{suggestion}"
    return output


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Percona Developer Knowledge",
    instructions=(
        "Search and retrieve Percona knowledge: official product documentation, "
        "posts from the Percona Community blog (percona.community/blog), and threads "
        "from the Percona forums (forums.percona.com). Use this for any Percona "
        "product question including configuration, troubleshooting, exact error "
        "messages, tuning advice, integration patterns, and migration experiences. "
        "Note: this connector reaches a shared instance on the Percona internal "
        "network and requires VPN access; tool calls will return a VPN-required "
        "message when the backend is unreachable."
    ),
)


@mcp.tool()
def search_percona_docs(query: str, top_k: int = 5, version: str | None = None) -> str:
    """Semantic search across Percona docs, blog, and forum threads.

    This tool searches a single combined corpus that includes:
      - Official Percona documentation (all product repos on GitHub)
      - Percona Community blog posts (percona.community/blog)
      - Percona forum threads (forums.percona.com) - real-world Q&A,
        troubleshooting discussions, and community-reported issues

    Use this tool for ANY Percona-related question: configuration,
    troubleshooting, exact error messages, tuning advice, integration
    patterns, migration experiences, version-specific quirks. You do NOT
    need to fall back to generic web search for forum or community
    discussions - they are already indexed here. Each result indicates
    its source (product doc repo, "percona-community-blog", or
    "percona-forums") so the caller can weigh official vs. community
    content appropriately.

    Note: this connector requires the Percona VPN. If the backend is
    unreachable, the tool returns a VPN-required message instead of
    search results.

    Note: Percona XtraBackup docs are in `pxb-docs`, NOT a repo named
    `xtrabackup-docs`. Percona Backup for MongoDB docs are in `pbm-docs`.
    The PostgreSQL distribution is `postgresql-docs`.

    Args:
        query: Natural language search query. Can include exact error
               strings ("WSREP: Failed to open backend connection"),
               product names, configuration flags, or full questions.
        top_k: Number of results to return (1-20, default 5).
        version: Optional product version to scope results to. Indexed:
                 PS / PXC / PXB / PDMySQL on "8.0" and "8.4"; PSMDB on
                 "6.0", "7.0", "8.0"; PostgreSQL on "16" and "17".
                 Operator docs (k8s*), PMM, PBM, Toolkit, Valkey,
                 pg_tde, pgsm, everest, etc. are single-branch and
                 ignore this arg. NOT indexed: PXB 2.4 / PS 5.7 (EOL),
                 MongoDB 5.0, PXB 9.x (~0.05% of deployed instances).
                 Pass this when a query mentions a specific version
                 (e.g. "PXB 8.0.35 release notes" -> version="8.0").
    """
    top_k = max(1, min(int(top_k), 20))
    payload: dict = {"query": query, "top_k": top_k}
    if version:
        payload["version"] = version
    try:
        resp = requests.post(
            f"{BACKEND_URL}/search",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return _format_search_results(resp.json())
    except requests.ConnectionError as e:
        log.warning("Backend unreachable: %s", e)
        return _vpn_error_with_detail(f"connection error reaching {BACKEND_URL}")
    except requests.Timeout:
        return _vpn_error_with_detail(
            f"request timed out after {REQUEST_TIMEOUT}s"
        )
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        return (
            f"Percona DK backend returned HTTP {status}.\n\n"
            f"Backend: `{BACKEND_URL}`\n\n*Body:* {body}"
        )
    except Exception as e:
        log.exception("Unexpected error in search_percona_docs")
        return f"Unexpected error calling Percona DK: {type(e).__name__}: {e}"


@mcp.tool()
def get_percona_doc(repo: str, path: str, version: str | None = None) -> str:
    """Retrieve the full content of a specific Percona doc, blog post,
    or forum thread.

    Use this when you already know which page you want (e.g. from a
    previous search result) and need the complete content.

    Note: Percona XtraBackup docs are in `pxb-docs`, NOT a repo named
    `xtrabackup-docs`. Percona Backup for MongoDB docs are in `pbm-docs`.

    Args:
        repo: Repository short name. Multi-version repos (require
              `version`): psmysql-docs, pxc-docs, pxb-docs,
              pdmysql-docs, psmdb-docs, postgresql-docs. Single-branch:
              pmm-doc, pbm-docs, k8sps-docs, k8spxc-docs, k8spsmdb-docs,
              k8spg-docs, percona-toolkit, pg_tde, pgsm-docs, pcsm-docs,
              percona-valkey-doc, ps-binlog-server-docs,
              proxysql-admin-tool-doc, pmm_dump_docs, repo-config-docs,
              everest-doc. Community: percona-community-blog,
              percona-forums.
        path: File path within the repo, e.g. 'docs/installation.md',
              'posts/{slug}.md', or 't/{topic_id}/{post_number}.md'.
        version: Required for multi-version repos (e.g. "8.0", "8.4",
              "7.0", "16"). Omit for single-branch repos. If you call a
              multi-version repo without this, the response lists the
              available versions so you can retry.
    """
    params = {}
    if version:
        params["version"] = version
    # URL-encode the repo segment so a full slug like "percona/pxb-docs"
    # doesn't get split on the literal slash by FastAPI's path router.
    # `safe=""` ensures the slash itself is encoded as %2F.
    from urllib.parse import quote
    repo_segment = quote(repo, safe="")
    try:
        resp = requests.get(
            f"{BACKEND_URL}/document/{repo_segment}/{path}",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "content" in data:
            return data["content"]
        return str(data)
    except requests.ConnectionError as e:
        log.warning("Backend unreachable: %s", e)
        return _vpn_error_with_detail(f"connection error reaching {BACKEND_URL}")
    except requests.Timeout:
        return _vpn_error_with_detail(
            f"request timed out after {REQUEST_TIMEOUT}s"
        )
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        if status == 404:
            return f"Document not found: `{repo}/{path}`"
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        return (
            f"Percona DK backend returned HTTP {status}.\n\n"
            f"Backend: `{BACKEND_URL}`\n\n*Body:* {body}"
        )
    except Exception as e:
        log.exception("Unexpected error in get_percona_doc")
        return f"Unexpected error calling Percona DK: {type(e).__name__}: {e}"


def main():
    """CLI entrypoint for `percona-dk-mcp-remote` (stdio MCP)."""
    log.info("Percona DK remote shim starting; backend=%s", BACKEND_URL)
    mcp.run()


if __name__ == "__main__":
    main()
