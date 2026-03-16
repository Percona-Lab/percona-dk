"""
Percona Developer Knowledge — Repo Registry

Maps known Percona doc repos to product keywords for suggesting
unconfigured repos when search results are weak.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# All known Percona doc repos and the keywords that indicate relevance.
# This is checked against the user's query when search results score low.
KNOWN_REPOS: dict[str, list[str]] = {
    "percona/psmysql-docs": [
        "percona server", "psmysql", "ps-mysql", "innodb", "xtradb",
        "mysql server", "percona mysql", "tokudb", "myrocks", "audit log",
        "thread pool", "encryption functions",
    ],
    "percona/pxc-docs": [
        "xtradb cluster", "pxc", "galera", "wsrep", "sst", "ist",
        "multi-master", "cluster replication", "garbd",
    ],
    "percona/pxb-docs": [
        "xtrabackup", "pxb", "backup", "incremental backup",
        "compressed backup", "prepare backup", "restore backup",
    ],
    "percona/pmm-doc": [
        "pmm", "percona monitoring", "monitoring and management",
        "grafana", "victoriametrics", "query analytics", "qan",
        "advisors", "alerting", "percona alerting",
    ],
    "percona/k8sps-docs": [
        "operator for mysql", "k8s mysql", "kubernetes mysql",
        "k8sps", "ps operator", "percona operator mysql",
    ],
    "percona/k8spxc-docs": [
        "operator for pxc", "k8s pxc", "kubernetes pxc",
        "k8spxc", "pxc operator", "percona operator pxc",
    ],
    "percona/percona-valkey-doc": [
        "valkey", "percona valkey", "key-value store",
    ],
    "percona/postgresql-docs": [
        "postgresql", "postgres", "pg_stat", "percona postgresql",
        "percona pg", "ppg",
    ],
    "percona/psmdb-docs": [
        "mongodb", "percona mongodb", "psmdb", "percona server mongodb",
        "mongod", "wiredtiger",
    ],
    "percona/pbm-docs": [
        "percona backup mongodb", "pbm", "mongodb backup",
    ],
    "percona/k8spsmdb-docs": [
        "operator for mongodb", "k8s mongodb", "kubernetes mongodb",
        "k8spsmdb", "psmdb operator", "percona operator mongodb",
    ],
    "percona/k8sppg-docs": [
        "operator for postgresql", "k8s postgresql", "kubernetes postgresql",
        "k8sppg", "ppg operator", "percona operator postgresql",
    ],
    "percona/percona-toolkit-doc": [
        "pt-online-schema-change", "pt-query-digest", "pt-table-checksum",
        "pt-table-sync", "pt-archiver", "pt-kill", "pt-stalk",
        "pt-summary", "pt-mysql-summary", "percona toolkit", "pt-",
    ],
    "percona/everest-doc": [
        "everest", "percona everest", "dbaas", "database as a service",
    ],
    "percona/proxysql-admin-tool-doc": [
        "proxysql", "proxy sql", "proxysql admin",
    ],
    "percona/orchestrator-docs": [
        "orchestrator", "replication topology", "failover",
    ],
}


def _get_configured_repos() -> set[str]:
    """Return the set of repo slugs the user has configured."""
    raw = os.getenv("REPOS", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def suggest_repos(query: str, max_score: float) -> str | None:
    """Check if the query matches a known but unconfigured repo.

    Always checks for keyword matches against unconfigured repos.
    Uses a two-tier message: softer when existing results are decent
    (the indexed docs mention the topic in passing), stronger when
    results are weak or missing.

    Args:
        query: The user's search query (lowercased internally).
        max_score: The highest relevance score from search results (0-1).

    Returns:
        A suggestion string, or None if no suggestion applies.
    """
    configured = _get_configured_repos()
    query_lower = query.lower()

    suggestions: list[str] = []
    for repo, keywords in KNOWN_REPOS.items():
        if repo in configured:
            continue
        for kw in keywords:
            if kw in query_lower:
                suggestions.append(repo)
                break

    if not suggestions:
        return None

    repos_str = ", ".join(f"`{r}`" for r in suggestions)

    if max_score > 0.6:
        # Decent results exist, but the actual source docs aren't indexed
        return (
            f"\n\n---\n**Note:** The results above mention this topic, but the "
            f"primary documentation lives in {repos_str}, which is not currently "
            f"in your configured repos. Add it to REPOS in your .env file and "
            f"run `percona-dk-ingest` for more complete results."
        )
    else:
        # Weak or no results
        return (
            f"\n\n---\n**Tip:** Your query may be relevant to {repos_str}, "
            f"which is not currently in your configured repos. "
            f"Add it to REPOS in your .env file and run `percona-dk-ingest` to index it."
        )
