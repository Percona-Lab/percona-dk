"""
Version check — compares local version against latest GitHub release.
Prints a notice on startup if a newer version is available.
"""

import logging
import urllib.request
import json

log = logging.getLogger(__name__)

REPO = "Percona-Lab/percona-dk"
LOCAL_VERSION = "0.1.0"


def check_for_update() -> str | None:
    """Check GitHub for a newer release. Returns update message or None."""
    try:
        url = f"https://api.github.com/repos/{REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        latest = data.get("tag_name", "").lstrip("v")
        if latest and latest != LOCAL_VERSION:
            return (
                f"percona-dk update available: v{LOCAL_VERSION} → v{latest}\n"
                f"  Run: cd percona-dk && git pull && pip install .\n"
                f"  Changelog: https://github.com/{REPO}/releases/tag/v{latest}"
            )
    except Exception:
        pass  # network errors are fine — don't block startup
    return None


def print_version_notice():
    """Print update notice to stderr if available."""
    msg = check_for_update()
    if msg:
        log.warning(msg)
