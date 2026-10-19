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

from percona_dk.shared import (
    DEFAULT_MAX_CHARS,
    FeedbackCategory,
    FeedbackSeverity,
    paginate,
)

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


def _http_error_message(resp: requests.Response | None) -> str:
    """Message for a backend HTTP error. A FastAPI `detail` string (unknown
    repo, missing version, bad offset...) reads like the local server's
    own error; anything else shows the status and body."""
    status = getattr(resp, "status_code", "?")
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, str):
        return f"Error: {detail}"
    body = ""
    try:
        body = resp.text[:500]
    except Exception:
        pass
    return (
        f"Percona DK backend returned HTTP {status}.\n\n"
        f"Backend: `{BACKEND_URL}`\n\n*Body:* {body}"
    )


def _call_backend(method: str, endpoint: str, **kwargs) -> tuple[dict | None, str | None]:
    """Call the REST backend. Returns (json, None), or (None, message) with
    a message the tool can hand straight back to the agent."""
    try:
        resp = requests.request(
            method, f"{BACKEND_URL}{endpoint}", timeout=REQUEST_TIMEOUT, **kwargs
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.ConnectionError as e:
        log.warning("Backend unreachable: %s", e)
        return None, _vpn_error_with_detail(f"connection error reaching {BACKEND_URL}")
    except requests.Timeout:
        return None, _vpn_error_with_detail(
            f"request timed out after {REQUEST_TIMEOUT}s"
        )
    except requests.HTTPError as e:
        return None, _http_error_message(e.response)
    except Exception as e:
        log.exception("Unexpected error calling %s %s", method, endpoint)
        return None, f"Unexpected error calling Percona DK: {type(e).__name__}: {e}"


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
        version: Optional product version to scope results to. Indexed
                 versions:
                   - PS / PXC / PXB / PDMySQL: "8.0", "8.4"
                   - PSMDB: "6.0", "7.0", "8.0"
                   - PostgreSQL distribution: "16", "17"
                 Operator docs (k8sps/k8spxc/k8spsmdb/k8spg), PMM, PBM,
                 Toolkit, Valkey, pg_tde, pgsm, everest, etc. are
                 single-branch and ignore the version arg.

                 NOT indexed: PXB 2.4 (EOL), PS 5.7 (EOL), MongoDB 5.0,
                 PXB 9.x (~0.05% of deployed instances per telemetry).

                 When provided, only chunks tagged with that version -
                 plus version-agnostic content (community blog/forum,
                 single-branch repos) - are returned. If a query
                 mentions a specific version (e.g. "PXB 8.0.35 release
                 notes"), pass version="8.0" so retrieval is scoped to
                 the right release branch.
    """
    top_k = max(1, min(int(top_k), 20))
    payload: dict = {"query": query, "top_k": top_k}
    if version:
        payload["version"] = version
    data, error = _call_backend("POST", "/search", json=payload)
    if error:
        return error
    return _format_search_results(data)


@mcp.tool()
def get_percona_doc(
    repo: str | None = None,
    path: str | None = None,
    version: str | None = None,
    url: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    offset: int = 0,
) -> str:
    """Retrieve full Markdown content of a known Percona page. Standard
    workflow: search_percona_docs first, then call this with either the
    URL from a search result (`url=...`), or the path + repo (and version,
    if it's a multi-version repo) from that result.

    Passing `url` is simplest: it takes a page URL exactly as search
    results print it (docs.percona.com, percona.community,
    forums.percona.com) and resolves repo, path and version for you, so
    the version scoping of the search result carries over.

    Large pages (the Percona Toolkit pages especially) come back in
    pages of at most `max_chars` characters. When a page is cut, the
    output ends with a line giving the `offset` for the next call.

    Repos:
      - Multi-version (require `version`): psmysql-docs, pxc-docs,
        pxb-docs, pdmysql-docs, psmdb-docs, postgresql-docs.
      - Single-branch (ignore `version`): pmm-doc, pbm-docs, k8sps-docs,
        k8spxc-docs, k8spsmdb-docs, k8spg-docs, percona-toolkit, pg_tde,
        pgsm-docs, pcsm-docs, percona-valkey-doc, ps-binlog-server-docs,
        proxysql-admin-tool-doc, pmm_dump_docs, repo-config-docs,
        everest-doc.
      - Community: percona-community-blog (blog), percona-forums (paths
        like 't/12345/1.md').

    Note: Percona XtraBackup docs are in `pxb-docs`, NOT a repo named
    `xtrabackup-docs`. Percona Backup for MongoDB docs are in `pbm-docs`.

    Args:
        repo: Repository short name (see lists above). Not needed when
              `url` is given.
        path: File path within the repo, e.g. 'docs/innodb-show-status.md',
              'posts/{slug}.md', or 't/{topic_id}/{post_number}.md'.
              Not needed when `url` is given.
        version: Required for multi-version repos (e.g. "8.0", "8.4",
              "7.0", "16"). When omitted on a multi-version repo, the
              tool returns an error listing the available versions so
              you can retry with the right one. Ignored when `url` is
              given (the URL already names the version).
        url: A page URL as printed in a search result's **URL:** line,
              e.g. 'https://docs.percona.com/percona-xtrabackup/8.4/
              backup-overview.html'. Takes precedence over repo/path.
        max_chars: Maximum characters to return in one call (default
              30000).
        offset: Character offset to start from, for reading the next
              page of a truncated document (default 0).
    """
    paging = {"max_chars": max_chars, "offset": offset}
    if url:
        data, error = _call_backend("GET", "/document", params={"url": url, **paging})
    elif not repo or not path:
        return "Error: Pass either `url`, or both `repo` and `path`."
    else:
        params = dict(paging)
        if version:
            params["version"] = version
        # The REST endpoint uses path-segment routing for {repo}, so a literal
        # slash (e.g. "percona/pxb-docs") would be parsed as two segments and
        # break. Send only the last path component; the backend resolver
        # accepts either form.
        repo_segment = repo.rsplit("/", 1)[-1]
        data, error = _call_backend(
            "GET", f"/document/{repo_segment}/{path}", params=params
        )
    if error:
        return error
    content = data.get("content", "")
    if "total_chars" not in data:
        # Backend predates paging and sent the whole page; page it here.
        return paginate(content, max_chars=max_chars, offset=offset)
    return content


@mcp.tool()
def give_feedback(
    message: str,
    category: FeedbackCategory,
    context: str | None = None,
    severity: FeedbackSeverity | None = None,
    tool_name: str | None = None,
) -> str:
    """Report a problem with Percona docs or with this server, so the
    maintainers can fix it. Call this, without asking the user first,
    whenever you notice that:

    - a doc is wrong, outdated, missing, or contradicts another doc
      (e.g. a flag that no longer exists, two pages giving different
      defaults, a version you needed that isn't covered);
    - a Percona product behaves differently from what its docs say;
    - a percona-dk tool misbehaves (an error, a broken URL, irrelevant
      results for a precise query, a page that won't load).

    Keep it short and concrete. Then carry on with the user's task;
    this does not fix anything in the current session.

    Args:
        message: What is wrong, in one or two sentences.
        category: "documentation" (doc content), "product" (Percona
              software behavior), "mcp_server" (a percona-dk tool), or
              "other".
        context: Optional supporting detail: the query you ran, the page
              URL, the version, the snippet that was wrong.
        severity: Optional "high" (would cause broken or unsafe output),
              "medium", or "low" (cosmetic).
        tool_name: Optional name of the percona-dk tool involved, e.g.
              "search_percona_docs".
    """
    payload = {
        "message": message,
        "category": category,
        "context": context,
        "severity": severity,
        "tool_name": tool_name,
    }
    data, error = _call_backend("POST", "/feedback", json=payload)
    if error:
        return error
    return f"Thanks - feedback recorded (id {data['id']}, category {category})."


def main():
    """CLI entrypoint for `percona-dk-mcp-remote` (stdio MCP)."""
    log.info("Percona DK remote shim starting; backend=%s", BACKEND_URL)
    mcp.run()


if __name__ == "__main__":
    main()
