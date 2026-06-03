"""
Tests for _build_page_url — the docs.percona.com URL constructor.

Guards two things that have bitten us:
  1. Doc-source folder prefixes must be stripped (pg_tde keeps docs under
     `documentation/docs/`, not `docs/`), or the prefix leaks into the URL.
  2. Repos whose docs site uses `use_directory_urls: false` (or Sphinx) must
     get `.html` URLs, not trailing-slash ones (the slash form 404s).

Run: python -m pytest tests/test_build_page_url.py -v
"""

import pytest

from percona_dk.ingest import _build_page_url


@pytest.mark.parametrize(
    "repo, path, version, expected",
    [
        # pg_tde: documentation/docs/ prefix stripped AND .html style.
        # Verified live: https://docs.percona.com/pg-tde/variables.html
        (
            "percona/pg_tde",
            "documentation/docs/variables.md",
            None,
            "https://docs.percona.com/pg-tde/variables.html",
        ),
        (
            "percona/pg_tde",
            "documentation/docs/release-notes/release-notes-v2.0.md",
            None,
            "https://docs.percona.com/pg-tde/release-notes/release-notes-v2.0.html",
        ),
        # Other .html (use_directory_urls: false) repos. Verified live.
        (
            "percona/pgsm-docs",
            "docs/comparison.md",
            None,
            "https://docs.percona.com/pg-stat-monitor/comparison.html",
        ),
        (
            "percona/percona-valkey-doc",
            "docs/installation.md",
            None,
            "https://docs.percona.com/valkey/installation.html",
        ),
        (
            "percona/percona-toolkit",
            "docs/pt-online-schema-change.rst",
            None,
            "https://docs.percona.com/percona-toolkit/pt-online-schema-change.html",
        ),
        # Directory-style (trailing slash) repos — unchanged behaviour.
        (
            "percona/pxc-docs",
            "docs/clone-sst.md",
            "8.4",
            "https://docs.percona.com/percona-xtradb-cluster/8.4/clone-sst/",
        ),
        (
            "percona/pbm-docs",
            "docs/usage/restore-physical.md",
            None,
            "https://docs.percona.com/percona-backup-mongodb/usage/restore-physical/",
        ),
        (
            "percona/k8spg-docs",
            "docs/update-db-major.md",
            None,
            "https://docs.percona.com/percona-operator-for-postgresql/update-db-major/",
        ),
        # Index pages collapse to the section/site root for both styles.
        (
            "percona/psmysql-docs",
            "docs/index.md",
            None,
            "https://docs.percona.com/percona-server/latest/",
        ),
        (
            "percona/pgsm-docs",
            "docs/index.md",
            None,
            "https://docs.percona.com/pg-stat-monitor/",
        ),
    ],
)
def test_build_page_url(repo, path, version, expected):
    assert _build_page_url(repo, path, version) == expected


def test_unmapped_repo_falls_back_to_github():
    # pcsm-docs is not published on docs.percona.com -> GitHub source link.
    url = _build_page_url("percona/pcsm-docs", "docs/limitations.md", None)
    assert url == "https://github.com/percona/pcsm-docs/blob/main/docs/limitations.md"
