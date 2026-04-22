#!/usr/bin/env python3
"""
Percona DK Installer - cross-platform installer for Percona DK.
Run with: uv run --python 3.12 installer.py
"""

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

if platform.system() == "Windows":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Enable VIRTUAL_TERMINAL_PROCESSING (0x0004) on stdout handle
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Repo definitions
# ---------------------------------------------------------------------------
REPO_NAMES: dict[str, str] = {
    "percona/psmysql-docs": "Percona Server for MySQL",
    "percona/pxc-docs": "Percona XtraDB Cluster",
    "percona/pxb-docs": "Percona XtraBackup",
    "percona/pdmysql-docs": "Percona Distribution for MySQL",
    "percona/ps-binlog-server-docs": "Percona Binlog Server",
    "percona/pmm-doc": "Percona Monitoring and Management",
    "percona/psmdb-docs": "Percona Server for MongoDB",
    "percona/pbm-docs": "Percona Backup for MongoDB",
    "percona/pcsm-docs": "Percona ClusterSync for MongoDB",
    "percona/postgresql-docs": "Percona Distribution for PostgreSQL",
    "percona/pg_tde": "pg_tde (Transparent Data Encryption)",
    "percona/pgsm-docs": "pg_stat_monitor",
    "percona/k8sps-docs": "Operator for MySQL",
    "percona/k8spxc-docs": "Operator for PXC",
    "percona/k8spsmdb-docs": "Operator for MongoDB",
    "percona/k8spg-docs": "Operator for PostgreSQL",
    "openeverest/everest-doc": "OpenEverest DBaaS Platform",
    "percona/proxysql-admin-tool-doc": "ProxySQL Admin Tool",
    "percona/percona-toolkit": "Percona Toolkit",
    "percona/percona-valkey-doc": "Percona Packages for Valkey",
    "percona/pmm_dump_docs": "PMM Dump",
    "percona/repo-config-docs": "Percona Software Repositories",
}

STACKS = [
    {
        "name": "MySQL stack",
        "repos": [
            "percona/psmysql-docs",
            "percona/pxc-docs",
            "percona/pxb-docs",
            "percona/pdmysql-docs",
            "percona/ps-binlog-server-docs",
        ],
    },
    {
        "name": "MongoDB stack",
        "repos": [
            "percona/psmdb-docs",
            "percona/pbm-docs",
            "percona/pcsm-docs",
        ],
    },
    {
        "name": "PostgreSQL stack",
        "repos": [
            "percona/postgresql-docs",
            "percona/pg_tde",
            "percona/pgsm-docs",
        ],
    },
    {
        "name": "Valkey",
        "repos": [
            "percona/percona-valkey-doc",
        ],
    },
    {
        "name": "Kubernetes Operators",
        "repos": [
            "percona/k8sps-docs",
            "percona/k8spxc-docs",
            "percona/k8spsmdb-docs",
            "percona/k8spg-docs",
        ],
    },
    {
        "name": "OpenEverest",
        "repos": [
            "openeverest/everest-doc",
        ],
    },
    {
        "name": "Tools and PMM",
        "repos": [
            "percona/pmm-doc",
            "percona/pmm_dump_docs",
            "percona/proxysql-admin-tool-doc",
            "percona/percona-toolkit",
            "percona/repo-config-docs",
        ],
    },
]

ALL_REPOS = [repo for stack in STACKS for repo in stack["repos"]]

REPO_URL = "https://github.com/Percona-Lab/percona-dk.git"

# Approximate doc file counts used as fallback when the GitHub API is unavailable.
# Updated periodically - good enough for time/disk estimates.
FALLBACK_MD_COUNTS: dict[str, int] = {
    "percona/psmysql-docs": 249,
    "percona/pxc-docs": 80,
    "percona/pxb-docs": 115,
    "percona/pdmysql-docs": 42,
    "percona/ps-binlog-server-docs": 6,
    "percona/pmm-doc": 228,
    "percona/psmdb-docs": 75,
    "percona/pbm-docs": 147,
    "percona/pcsm-docs": 40,
    "percona/postgresql-docs": 69,
    "percona/pg_tde": 61,
    "percona/pgsm-docs": 31,
    "percona/k8sps-docs": 84,
    "percona/k8spxc-docs": 144,
    "percona/k8spsmdb-docs": 175,
    "percona/k8spg-docs": 121,
    "percona/percona-valkey-doc": 25,
    "openeverest/everest-doc": 122,
    "percona/proxysql-admin-tool-doc": 62,
    "percona/percona-toolkit": 30,
    "percona/pmm_dump_docs": 35,
    "percona/repo-config-docs": 15,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def c(color: str, text: str) -> str:
    """Wrap text in ANSI color codes."""
    return f"{color}{text}{NC}"


def info(msg: str) -> None:
    print(f"{GREEN}  {msg}{NC}")


def warn(msg: str) -> None:
    print(f"{YELLOW}  Warning: {msg}{NC}")


def error(msg: str) -> None:
    print(f"{RED}  Error: {msg}{NC}")


def die(msg: str) -> None:
    error(msg)
    sys.exit(1)


def ask(prompt: str, default: str = "") -> str:
    """Prompt user for input, returning default on empty input or EOF."""
    display_default = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{display_default}: ").strip()
        return value if value else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def ask_yn(prompt: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer."""
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"  {prompt} ({hint}): ").strip().lower()
        if not value:
            return default
        return value in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def run(cmd: list, cwd: Path = None, env: dict = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command."""
    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        check=check,
    )


def python_in_venv(venv: Path) -> Path:
    """Return path to python executable inside a venv."""
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def scripts_dir(venv: Path) -> Path:
    """Return the scripts/bin directory for a venv."""
    if platform.system() == "Windows":
        return venv / "Scripts"
    return venv / "bin"

# ---------------------------------------------------------------------------
# Step 1: Banner
# ---------------------------------------------------------------------------

def print_banner() -> None:
    print()
    print(c(BOLD, "=" * 60))
    print(c(BOLD, " Percona DK Installer"))
    print(c(BOLD, " Developer Knowledge - Percona docs in your AI assistant"))
    print(c(BOLD, "=" * 60))
    print()

# ---------------------------------------------------------------------------
# Step 2: Check prerequisites
# ---------------------------------------------------------------------------

def check_prerequisites() -> None:
    print(c(BOLD, "Checking prerequisites..."))

    try:
        subprocess.run(
            ["git", "--version"],
            check=True,
            capture_output=True,
        )
        info("git found")
    except (subprocess.CalledProcessError, FileNotFoundError):
        die(
            "git is not installed or not in PATH. "
            "Install git from https://git-scm.com/ and re-run this installer."
        )

    try:
        subprocess.run(
            ["uv", "--version"],
            check=True,
            capture_output=True,
        )
        info("uv found")
    except (subprocess.CalledProcessError, FileNotFoundError):
        die(
            "uv is not installed or not in PATH. "
            "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and re-run this installer."
        )

    print()

# ---------------------------------------------------------------------------
# Step 3: Install directory
# ---------------------------------------------------------------------------

def get_install_dir() -> tuple[Path, bool]:
    """Return (install_dir, is_rerun)."""
    print(c(BOLD, "Install location"))
    default = Path.home() / "percona-dk"
    raw = ask("Install directory", str(default))
    install_dir = Path(raw).expanduser().resolve()

    is_rerun = (install_dir / ".git").exists()
    if is_rerun:
        warn(f"Existing installation detected at {install_dir} - will update.")
    else:
        info(f"Will install to {install_dir}")

    print()
    return install_dir, is_rerun

# ---------------------------------------------------------------------------
# Step 4: Clone or pull
# ---------------------------------------------------------------------------

def clone_or_pull(install_dir: Path, is_rerun: bool) -> None:
    print(c(BOLD, "Setting up repository..."))

    if is_rerun:
        info(f"Pulling latest changes in {install_dir}")
        run(["git", "pull"], cwd=install_dir)
    else:
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        info(f"Cloning {REPO_URL} into {install_dir}")
        run(["git", "clone", REPO_URL, str(install_dir)])

    print()

# ---------------------------------------------------------------------------
# Step 5: Create venv and install package
# ---------------------------------------------------------------------------

def setup_venv(install_dir: Path) -> None:
    print(c(BOLD, "Setting up Python environment..."))

    venv = install_dir / ".venv"

    info("Creating virtual environment with Python 3.12...")
    run(
        ["uv", "venv", str(venv), "--python", "3.12", "--clear", "--quiet"],
        cwd=install_dir,
    )

    py = python_in_venv(venv)
    info("Installing percona-dk package (this may take a few minutes)...")
    run(
        ["uv", "pip", "install", "-e", ".", "--python", str(py)],
        cwd=install_dir,
    )

    info("Environment ready.")
    print()

# ---------------------------------------------------------------------------
# Step 6: .env setup
# ---------------------------------------------------------------------------

def setup_env_file(install_dir: Path) -> tuple[list, int]:
    """Copy .env.example if needed. Return (existing_repos, existing_refresh_days)."""
    env_path = install_dir / ".env"
    example_path = install_dir / ".env.example"

    if not env_path.exists():
        if example_path.exists():
            shutil.copy(example_path, env_path)
            info("Created .env from .env.example")
        else:
            env_path.write_text("REPOS=\nREFRESH_DAYS=7\n")
            info("Created default .env")

    content = env_path.read_text()

    # Parse existing REPOS=
    existing_repos: list = []
    repos_match = re.search(r"^REPOS=(.*)$", content, re.MULTILINE)
    if repos_match:
        raw_repos = repos_match.group(1).strip()
        if raw_repos:
            existing_repos = [r.strip() for r in raw_repos.split(",") if r.strip()]

    # Parse existing REFRESH_DAYS=
    existing_refresh = 7
    refresh_match = re.search(r"^REFRESH_DAYS=(\d+)$", content, re.MULTILINE)
    if refresh_match:
        existing_refresh = int(refresh_match.group(1))

    return existing_repos, existing_refresh

# ---------------------------------------------------------------------------
# Step 7: Fetch .md file counts from GitHub API in parallel
# ---------------------------------------------------------------------------

def fetch_md_count(repo_slug: str, results: dict, lock: threading.Lock, rate_limited: list) -> None:
    """Fetch .md file count for a repo and store in results dict."""
    owner, repo = repo_slug.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "percona-dk-installer",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(url, headers=headers)
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        count = sum(
            1 for item in data.get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").endswith(".md")
        )
        with lock:
            results[repo_slug] = count
    except HTTPError as e:
        with lock:
            if e.code in (403, 429):
                rate_limited.append(repo_slug)
            results[repo_slug] = None
    except Exception:
        with lock:
            results[repo_slug] = None


def fetch_all_md_counts() -> dict:
    """Fetch .md counts for all repos in parallel. Returns {slug: count_or_None}."""
    print(c(BOLD, "Fetching repository sizes from GitHub..."))
    results = {}
    lock = threading.Lock()
    rate_limited: list = []

    threads = []
    for i, repo_slug in enumerate(ALL_REPOS):
        if i > 0:
            time.sleep(0.4)  # stagger requests to avoid GitHub secondary rate limiting
        t = threading.Thread(target=fetch_md_count, args=(repo_slug, results, lock, rate_limited), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    if rate_limited:
        print(f"  {YELLOW}GitHub API rate limit reached - using approximate estimates.{NC}")
        print(f"  {DIM}Set GITHUB_TOKEN in your environment for live counts.{NC}")

    # Fill in fallback counts for any repos that failed
    for slug in ALL_REPOS:
        if results.get(slug) is None:
            results[slug] = FALLBACK_MD_COUNTS.get(slug)

    print()
    return results


def time_estimate(md_count) -> str:
    """Return human-readable time estimate for indexing a repo."""
    if md_count is None:
        return "? min"
    mins = max(1, math.ceil(md_count * 7.5 / 1000))
    return f"~{mins} min"

# ---------------------------------------------------------------------------
# Step 8: Stack selection
# ---------------------------------------------------------------------------

def select_repos(md_counts: dict, existing_repos: list) -> list:
    """Interactively select repos. Returns list of selected slugs."""
    while True:
        selected = _run_selection(md_counts, existing_repos)
        if not selected:
            warn("No repos selected.")
            if not ask_yn("Go back and select repos?", default=True):
                die("No repos selected - nothing to install.")
            continue

        # Review
        print(c(BOLD, "Review selection"))
        print()
        total_docs = 0
        for slug in selected:
            count = md_counts.get(slug)
            count_str = str(count) if count is not None else "?"
            est = time_estimate(count)
            if count is not None:
                total_docs += count
            name = REPO_NAMES.get(slug, slug.split("/")[1])
            print(f"    {name:<42} {DIM}{slug:<40}{NC}  {count_str} docs, {est}")
        print()
        total_mins = max(1, math.ceil(total_docs * 7.5 / 1000))
        disk_mb = total_docs // 2
        print(f"  Total: {len(selected)} repo(s), ~{total_mins} min to index, ~{disk_mb}MB disk")
        print()

        if not ask_yn("Modify selection?", default=False):
            return selected


def _run_selection(md_counts: dict, existing_repos: list) -> list:
    """Run the stack selection UI once. Returns list of selected slugs."""
    selected = []

    print(c(BOLD, "Select documentation stacks to install"))
    print()

    for stack in STACKS:
        repos = stack["repos"]
        stack_docs = sum(
            md_counts.get(r, 0) or 0 for r in repos
        )
        stack_mins = max(1, math.ceil(stack_docs * 7.5 / 1000))

        print(f"  {c(BOLD, stack['name'])} (~{stack_mins} min total)")
        print(f"    1) Install entire stack")
        print(f"    2) Skip")
        print(f"    3) Choose individually")

        choice = ask("Choice", default="1")

        if choice == "2":
            info(f"Skipping {stack['name']}")
        elif choice == "3":
            print(f"  Choose repos for {stack['name']}:")
            for repo in repos:
                count = md_counts.get(repo)
                count_str = str(count) if count is not None else "?"
                est = time_estimate(count)
                name = REPO_NAMES.get(repo, repo.split("/")[1])
                yn = ask_yn(f"  Include {name} ({count_str} docs, {est})?", default=True)
                if yn:
                    selected.append(repo)
        else:
            # Default: install entire stack
            for repo in repos:
                selected.append(repo)
            info(f"Selected entire {stack['name']}")

        print()

    return selected

# ---------------------------------------------------------------------------
# Step 9: Refresh interval
# ---------------------------------------------------------------------------

def ask_refresh_days(existing_refresh: int) -> int:
    raw = ask("Check for doc updates every N days (0 to disable)", default=str(existing_refresh))
    try:
        return int(raw)
    except ValueError:
        warn(f"Invalid value '{raw}', using {existing_refresh}")
        return existing_refresh

# ---------------------------------------------------------------------------
# Step 10: Write .env
# ---------------------------------------------------------------------------

def write_env(install_dir: Path, selected_repos: list, refresh_days: int) -> None:
    env_path = install_dir / ".env"
    content = env_path.read_text()

    repos_value = ",".join(selected_repos)

    # Update or append REPOS=
    if re.search(r"^REPOS=", content, re.MULTILINE):
        content = re.sub(r"^REPOS=.*$", f"REPOS={repos_value}", content, flags=re.MULTILINE)
    else:
        content += f"\nREPOS={repos_value}\n"

    # Update or append REFRESH_DAYS=
    if re.search(r"^REFRESH_DAYS=", content, re.MULTILINE):
        content = re.sub(r"^REFRESH_DAYS=.*$", f"REFRESH_DAYS={refresh_days}", content, flags=re.MULTILINE)
    else:
        content += f"\nREFRESH_DAYS={refresh_days}\n"

    env_path.write_text(content)
    info(f"Updated {env_path}")
    print()

# ---------------------------------------------------------------------------
# Step 11: AI client configuration
# ---------------------------------------------------------------------------

def build_mcp_entry(install_dir: Path) -> dict:
    """Return the Claude Desktop / Claude Code MCP config for this install.

    Uses the local-stdio remote shim (percona_dk.mcp_remote_shim), which
    forwards tool calls to the shared Percona DK instance on sherpa via
    its REST API. Off-VPN, tool calls return a friendly "VPN required"
    message instead of the client showing "Server disconnected". The
    connector itself stays green because the stdio MCP process keeps
    running regardless of network state.
    """
    venv = install_dir / ".venv"
    py = python_in_venv(venv)
    env_path = install_dir / ".env"
    return {
        "command": str(py),
        "args": ["-m", "percona_dk.mcp_remote_shim"],
        "env": {"DOTENV_PATH": str(env_path)},
    }


def _configure_mcp_client(name: str, config_path: Path, install_dir: Path, detected: bool) -> bool:
    """Configure an MCP client. Returns True if configured."""
    if not detected:
        print(f"  {DIM}{name} not detected ({config_path.parent}){NC}")
        if not ask_yn(f"Configure {name} MCP anyway?", default=False):
            return False

    if not config_path.parent.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {config_path} - will overwrite.")

    config.setdefault("mcpServers", {})
    config["mcpServers"]["percona-dk"] = build_mcp_entry(install_dir)

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    info(f"Configured {name}: {config_path}")
    return True


def _get_ai_clients() -> list[dict]:
    """Return list of known AI clients with their config paths."""
    system = platform.system()
    home = Path.home()

    clients = []

    # Claude Desktop
    if system == "Darwin":
        desktop_path = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif system == "Linux":
        desktop_path = home / ".config" / "Claude" / "claude_desktop_config.json"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        desktop_path = Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    else:
        desktop_path = None
    if desktop_path:
        clients.append({"name": "Claude Desktop", "path": desktop_path})

    # Claude Code
    clients.append({"name": "Claude Code", "path": home / ".claude" / "settings.json"})

    # Cursor
    clients.append({"name": "Cursor", "path": home / ".cursor" / "mcp.json"})

    # Windsurf
    clients.append({"name": "Windsurf", "path": home / ".codeium" / "windsurf" / "mcp_config.json"})

    return clients


def configure_ai_clients(install_dir: Path) -> bool:
    """Configure all detected AI clients. Returns True if any were configured."""
    print(c(BOLD, "Configuring AI clients..."))
    any_configured = False
    configured_names = []

    for client in _get_ai_clients():
        name = client["name"]
        config_path = client["path"]
        detected = config_path.parent.exists()

        if detected:
            info(f"{name} detected - auto-configuring...")

        if _configure_mcp_client(name, config_path, install_dir, detected):
            any_configured = True
            configured_names.append(name)

    if configured_names:
        print()
        info(f"Percona DK is ready to use in: {', '.join(configured_names)}")

    print()
    return any_configured

# ---------------------------------------------------------------------------
# Step 12: Ingestion
# ---------------------------------------------------------------------------

def run_ingestion(install_dir: Path, selected_repos: list, existing_repos: list, md_counts: dict) -> None:
    print(c(BOLD, "Document ingestion"))

    chroma_dir = install_dir / "data" / "chroma"
    env_path = install_dir / ".env"

    if platform.system() == "Windows":
        ingest_bin = scripts_dir(install_dir / ".venv") / "percona-dk-ingest.exe"
    else:
        ingest_bin = scripts_dir(install_dir / ".venv") / "percona-dk-ingest"

    env = {"DOTENV_PATH": str(env_path)}

    total_docs = sum(md_counts.get(r, 0) or 0 for r in selected_repos)
    total_mins = max(1, math.ceil(total_docs * 7.5 / 1000)) if total_docs else None
    disk_mb = total_docs // 2
    est_str = f"~{total_mins} min, ~{disk_mb}MB" if total_mins else "? min"

    if chroma_dir.exists():
        repos_changed = set(selected_repos) != set(existing_repos)

        # Check how long ago the last ingest ran
        marker = install_dir / "data" / ".last_ingest"
        ingest_age_days = None
        if marker.exists():
            try:
                ingest_age_days = (time.time() - float(marker.read_text().strip())) / 86400
            except Exception:
                pass

        if repos_changed:
            warn("Repo selection changed - re-indexing recommended.")
        elif ingest_age_days is not None:
            age_str = f"{int(ingest_age_days)}d" if ingest_age_days >= 1 else f"{int(ingest_age_days * 24)}h"
            print(f"  {DIM}Index exists (last run: {age_str} ago).{NC}")
        else:
            print(f"  {DIM}Index exists.{NC}")

        do_ingest = ask_yn(f"Re-index now? ({est_str})", default=True)
    else:
        print(f"  No index found.")
        do_ingest = ask_yn(f"Run ingestion now? ({est_str})", default=True)

    if do_ingest:
        info("Starting ingestion - this may take a while...")
        try:
            run([str(ingest_bin)], cwd=install_dir, env=env)
            info("Ingestion complete.")
        except subprocess.CalledProcessError as e:
            warn(f"Ingestion exited with code {e.returncode}. Check logs for details.")
        except FileNotFoundError:
            warn(f"Ingestion binary not found at {ingest_bin}. Try running manually.")
    else:
        info("Skipping ingestion - you can run it later with:")
        print(f"    {DIM}DOTENV_PATH={env_path} {ingest_bin}{NC}")

    print()

# ---------------------------------------------------------------------------
# Install analytics (fire-and-forget, best-effort, privacy-preserving)
# ---------------------------------------------------------------------------

ANALYTICS_URL = os.getenv(
    "PERCONA_DK_ANALYTICS_URL",
    "https://script.google.com/macros/s/AKfycbyPZGQMhUy1MFdbVNDrasAYDV_c-PMw_GhGRYAnCUSWyXU5NRc76iYdKg7huj_8_k0/exec",
)


def _get_machine_hash() -> str:
    """Return a SHA-256 hash of a stable machine identifier. Never sends raw ID."""
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(
                ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
                text=True, timeout=5,
            )
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    raw = line.split('"')[-2]
                    return hashlib.sha256(raw.encode()).hexdigest()
        elif platform.system() == "Linux":
            for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
                if os.path.exists(path):
                    raw = open(path).read().strip()
                    return hashlib.sha256(raw.encode()).hexdigest()
        elif platform.system() == "Windows":
            out = subprocess.check_output(
                ["wmic", "csproduct", "get", "UUID"],
                text=True, timeout=5,
            )
            raw = out.strip().splitlines()[-1].strip()
            return hashlib.sha256(raw.encode()).hexdigest()
    except Exception:
        pass
    # Fallback: random per-install hash (won't dedup but still counts)
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _get_dk_version(install_dir: Path) -> str:
    """Try to read the installed percona-dk version."""
    try:
        toml_path = install_dir / "pyproject.toml"
        if toml_path.exists():
            for line in toml_path.read_text().splitlines():
                if line.strip().startswith("version"):
                    return line.split("=")[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def send_install_analytics(install_dir: Path, selected_repos: list) -> None:
    """Fire-and-forget install analytics. Runs in background thread, never blocks."""


    def _send():
        try:
            stacks_used = set()
            for stack in STACKS:
                if any(r in selected_repos for r in stack["repos"]):
                    stacks_used.add(stack["name"])

            payload = json.dumps({
                "action": "install",
                "machine_hash": _get_machine_hash(),
                "app_version": _get_dk_version(install_dir),
                "os_version": f"{platform.system()} {platform.release()}",
                "platform": platform.machine(),
                "repo_count": len(selected_repos),
                "stacks": ",".join(sorted(stacks_used)),
            }).encode()

            req = Request(ANALYTICS_URL, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            urlopen(req, timeout=10)
        except Exception:
            pass  # Best-effort, never fail the install

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Step 13: Done
# ---------------------------------------------------------------------------

def print_done(any_clients_configured: bool) -> None:
    print(c(BOLD, "=" * 60))
    print(c(GREEN + BOLD, " Installation complete!"))
    print(c(BOLD, "=" * 60))
    print()
    if any_clients_configured:
        print(f"  {YELLOW}Restart your AI assistant (Claude Desktop / Claude Code){NC}")
        print("  for the Percona DK MCP server to take effect.")
        print()
    print("  The percona-dk MCP server is ready to use.")
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print_banner()

    # Step 2: Prerequisites
    check_prerequisites()

    # Step 3: Install directory
    install_dir, is_rerun = get_install_dir()

    # Step 4: Clone or pull
    clone_or_pull(install_dir, is_rerun)

    # Step 5: Venv + package
    setup_venv(install_dir)

    # Step 6: .env
    existing_repos, existing_refresh = setup_env_file(install_dir)

    # Step 7: Fetch MD counts in parallel
    md_counts = fetch_all_md_counts()

    # Step 8: Stack selection
    selected_repos = select_repos(md_counts, existing_repos)

    # Step 9: Refresh days
    print(c(BOLD, "Auto-refresh settings"))
    refresh_days = ask_refresh_days(existing_refresh)
    print()

    # Step 10: Write .env
    write_env(install_dir, selected_repos, refresh_days)

    # Step 11: AI clients
    any_configured = configure_ai_clients(install_dir)

    # Step 12: Ingestion
    run_ingestion(install_dir, selected_repos, existing_repos, md_counts)

    # Step 12b: Install analytics (fire-and-forget)
    send_install_analytics(install_dir, selected_repos)

    # Step 13: Done
    print_done(any_configured)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print(f"\n{YELLOW}  Installation cancelled.{NC}")
        sys.exit(1)
