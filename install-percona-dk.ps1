# Percona DK Installer for Windows
# Usage: irm https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/install-percona-dk.ps1 | iex

$ErrorActionPreference = "Stop"

$InstallerUrl = "https://raw.githubusercontent.com/Percona-Lab/percona-dk/main/installer.py"

Write-Host ""
Write-Host "Percona DK Installer" -ForegroundColor White
Write-Host ""

# ── Install uv if needed ───────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing uv..." -ForegroundColor Yellow
    $uvInstallScript = (Invoke-RestMethod "https://astral.sh/uv/install.ps1")
    Invoke-Expression $uvInstallScript

    # Refresh PATH for this session
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "  uv installed but not in PATH. Please restart your shell and re-run this installer." -ForegroundColor Red
        exit 1
    }
    Write-Host "  uv installed." -ForegroundColor Green
}

# ── Download and run the Python installer ─────────────────────
$TmpDir = [System.IO.Path]::GetTempPath() + [System.Guid]::NewGuid().ToString()
New-Item -ItemType Directory -Path $TmpDir | Out-Null

try {
    Write-Host "  Downloading installer..." -ForegroundColor DarkGray
    Invoke-WebRequest -Uri $InstallerUrl -OutFile "$TmpDir\installer.py"

    Write-Host "  Starting installer..."
    Write-Host ""
    uv run --python 3.12 "$TmpDir\installer.py"
} finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
