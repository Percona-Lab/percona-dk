#!/usr/bin/env python3
"""
Percona DK Installer - cross-platform installer for Percona DK.
Run with: uv run --python 3.12 installer.py
"""

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
from pathlib import Path
from urllib.error import URLError
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
STACKS = [
    {
        "name": "MySQL stack",
        "repos": [
            "percona/psmysql-docs",
            "percona/pxc-docs",
            "percona/pxb-docs",
            "percona/pmm-doc",
        ],
    },
    {
        "name": "MongoDB stack",
        "repos": [
            "percona/psmdb-docs",
            "percona/pbm-docs",
        ],
    },
    {
        "name": "PostgreSQL stack",
        "repos": [
            "percona/postgresql-docs",
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
        "name": "Tools",
        "repos": [
            "percona/proxysql-admin-tool-doc",
        ],
    },
]

ALL_REPOS = [repo for stack in STACKS for repo in stack["repos"]]

REPO_URL = "https://github.com/Percona-Lab/percona-dk.git"

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
    info("Installing percona-dk package...")
    run(
        ["uv", "pip", "install", "--quiet", "-e", ".", "--python", str(py)],
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

def fetch_md_count(repo_slug: str, results: dict, lock: threading.Lock) -> None:
    """Fetch .md file count for a repo and store in results dict."""
    owner, repo = repo_slug.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    try:
        req = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "percona-dk-installer",
        })
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        count = sum(
            1 for item in data.get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").endswith(".md")
        )
        with lock:
            results[repo_slug] = count
    except Exception:
        with lock:
            results[repo_slug] = None


def fetch_all_md_counts() -> dict:
    """Fetch .md counts for all repos in parallel. Returns {slug: count_or_None}."""
    print(c(BOLD, "Fetching repository sizes from GitHub..."))
    results = {}
    lock = threading.Lock()

    threads = []
    for i, repo_slug in enumerate(ALL_REPOS):
        if i > 0:
            time.sleep(0.1)  # stagger requests to avoid GitHub rate limiting
        t = threading.Thread(target=fetch_md_count, args=(repo_slug, results, lock), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # Report any failures
    failed = [slug for slug, v in results.items() if v is None]
    if failed:
        warn(f"Could not fetch sizes for: {', '.join(failed)}")

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
            short = slug.split("/")[1]
            print(f"    {DIM}{slug:<40}{NC}  {count_str} docs, {est}")
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
                short = repo.split("/")[1]
                # Default to Y if repo was previously selected, else N
                was_selected = repo in existing_repos
                yn = ask_yn(f"  Include {short} ({count_str} docs, {est})?", default=was_selected)
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
    raw = ask("Re-index docs every N days (0 to disable)", default=str(existing_refresh))
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

def get_claude_desktop_config_path() -> Path | None:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif system == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return None


def get_claude_code_config_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def build_mcp_entry(install_dir: Path) -> dict:
    venv = install_dir / ".venv"
    py = python_in_venv(venv)
    env_path = install_dir / ".env"
    return {
        "command": str(py),
        "args": ["-m", "percona_dk.mcp_server"],
        "env": {"DOTENV_PATH": str(env_path)},
    }


def configure_claude_desktop(config_path: Path, install_dir: Path) -> bool:
    """Configure Claude Desktop. Returns True if configured."""
    if not config_path.parent.exists():
        if not ask_yn(
            f"Claude Desktop config dir not found at {config_path.parent}.\n  Configure anyway?",
            default=False,
        ):
            return False
        config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or start fresh
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {config_path} - will overwrite.")

    config.setdefault("mcpServers", {})
    config["mcpServers"]["percona-dk"] = build_mcp_entry(install_dir)

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    info(f"Configured Claude Desktop: {config_path}")
    return True


def configure_claude_code(config_path: Path, install_dir: Path) -> bool:
    """Configure Claude Code (settings.json). Returns True if configured."""
    if not config_path.parent.exists():
        if not ask_yn(
            f"Claude Code config dir not found at {config_path.parent}.\n  Configure anyway?",
            default=False,
        ):
            return False
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
    info(f"Configured Claude Code: {config_path}")
    return True


def configure_ai_clients(install_dir: Path) -> bool:
    """Configure all detected AI clients. Returns True if any were configured."""
    print(c(BOLD, "Configuring AI clients..."))
    any_configured = False

    # Claude Desktop
    desktop_path = get_claude_desktop_config_path()
    if desktop_path is not None:
        if desktop_path.parent.exists():
            info("Claude Desktop detected - auto-configuring...")
            if configure_claude_desktop(desktop_path, install_dir):
                any_configured = True
        else:
            print(f"  {DIM}Claude Desktop not detected ({desktop_path.parent}){NC}")
            if ask_yn("Configure Claude Desktop MCP anyway?", default=False):
                if configure_claude_desktop(desktop_path, install_dir):
                    any_configured = True

    # Claude Code
    code_path = get_claude_code_config_path()
    if code_path.parent.exists():
        info("Claude Code detected - auto-configuring...")
        if configure_claude_code(code_path, install_dir):
            any_configured = True
    else:
        print(f"  {DIM}Claude Code not detected ({code_path.parent}){NC}")
        if ask_yn("Configure Claude Code MCP anyway?", default=False):
            if configure_claude_code(code_path, install_dir):
                any_configured = True

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

    if chroma_dir.exists():
        repos_changed = set(selected_repos) != set(existing_repos)
        if repos_changed:
            warn("Repo selection changed - existing index may be stale.")
            do_ingest = ask_yn("Re-index now?", default=True)
        else:
            print(f"  {DIM}Index already exists.{NC}")
            do_ingest = ask_yn("Re-index now?", default=False)
    else:
        total_docs = sum(md_counts.get(r, 0) or 0 for r in selected_repos)
        total_mins = max(1, math.ceil(total_docs * 7.5 / 1000))
        print(f"  No index found. Indexing will take ~{total_mins} min.")
        do_ingest = ask_yn("Run ingestion now?", default=True)

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

    # Step 13: Done
    print_done(any_configured)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print(f"\n{YELLOW}  Installation cancelled.{NC}")
        sys.exit(1)
