"""
Tests for _build_page_url — the docs.percona.com URL constructor.

docs.percona.com serves every product as static `.html`, so all published
URLs end in `.html` (a trailing-slash URL only works via a redirect). The
builder must also:
  - strip the repo's doc-source prefix (docs/, source/, or pg_tde's
    documentation/docs/), or the prefix leaks into the URL;
  - apply the right version segment per product (PMM /3/, multi-version DB
    repos by branch, PG operator /latest/, others unversioned);
  - collapse index pages to the section/site root.

All expected URLs below were checked against live docs.percona.com pages.

Run: python -m pytest tests/test_build_page_url.py -v
"""

import pytest

from percona_dk.ingest import _build_page_url


@pytest.mark.parametrize(
    "repo, path, version, expected",
    [
        # pg_tde: documentation/docs/ prefix stripped, .html.
        ("percona/pg_tde", "documentation/docs/variables.md", None,
         "https://docs.percona.com/pg-tde/variables.html"),
        ("percona/pg_tde", "documentation/docs/release-notes/release-notes-v2.0.md", None,
         "https://docs.percona.com/pg-tde/release-notes/release-notes-v2.0.html"),
        # Multi-version DB repos: branch in the path, .html.
        ("percona/pxc-docs", "docs/clone-sst.md", "8.4",
         "https://docs.percona.com/percona-xtradb-cluster/8.4/clone-sst.html"),
        ("percona/psmysql-docs", "docs/security/data-at-rest-encryption.md", "8.0",
         "https://docs.percona.com/percona-server/8.0/security/data-at-rest-encryption.html"),
        # PMM: fixed /3/ version segment.
        ("percona/pmm-doc", "docs/setting-up/client/mongodb.md", None,
         "https://docs.percona.com/percona-monitoring-and-management/3/setting-up/client/mongodb.html"),
        # Operators: Mongo / PS / PXC are unversioned...
        ("percona/k8spsmdb-docs", "docs/backups.md", None,
         "https://docs.percona.com/percona-operator-for-mongodb/backups.html"),
        ("percona/k8sps-docs", "docs/operator.md", None,
         "https://docs.percona.com/percona-operator-for-mysql/ps/operator.html"),
        # ...but the PG operator requires a version segment (/latest/).
        ("percona/k8spg-docs", "docs/update-db-major.md", None,
         "https://docs.percona.com/percona-operator-for-postgresql/latest/update-db-major.html"),
        # Unversioned standalone products (use_directory_urls:false / Sphinx).
        ("percona/pgsm-docs", "docs/comparison.md", None,
         "https://docs.percona.com/pg-stat-monitor/comparison.html"),
        ("percona/percona-valkey-doc", "docs/installation.md", None,
         "https://docs.percona.com/valkey/installation.html"),
        ("percona/percona-toolkit", "docs/pt-online-schema-change.rst", None,
         "https://docs.percona.com/percona-toolkit/pt-online-schema-change.html"),
        ("openeverest/everest-doc", "docs/index.md", None,
         "https://docs.percona.com/everest/"),
        ("percona/pbm-docs", "docs/usage/restore-physical.md", None,
         "https://docs.percona.com/percona-backup-mongodb/usage/restore-physical.html"),
        # Newly mapped (were wrongly falling back to GitHub).
        ("percona/pcsm-docs", "docs/limitations.md", None,
         "https://docs.percona.com/percona-clustersync-for-mongodb/limitations.html"),
        ("percona/pmm_dump_docs", "docs/export.md", None,
         "https://docs.percona.com/pmm-dump-documentation/export.html"),
        ("percona/repo-config-docs", "docs/percona-release.md", None,
         "https://docs.percona.com/percona-software-repositories/percona-release.html"),
        # Index pages collapse to the section/site root.
        ("percona/psmysql-docs", "docs/index.md", "8.4",
         "https://docs.percona.com/percona-server/8.4/"),
        ("percona/pgsm-docs", "docs/index.md", None,
         "https://docs.percona.com/pg-stat-monitor/"),
    ],
)
def test_build_page_url(repo, path, version, expected):
    assert _build_page_url(repo, path, version) == expected


def test_unmapped_repo_falls_back_to_github():
    # ps-binlog-server-docs is not published on docs.percona.com.
    url = _build_page_url("percona/ps-binlog-server-docs", "docs/index.md", None)
    assert url == "https://github.com/percona/ps-binlog-server-docs/blob/main/docs/index.md"
