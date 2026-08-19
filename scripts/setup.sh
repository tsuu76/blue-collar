#!/usr/bin/env bash
# IT Job Hunter — setup (spec section 32)
#
# Beginner-friendly, one-shot setup. Every step prints what it's doing and
# what failed (never hides an error) — if something can't be fixed
# automatically, the script tells you exactly what command to run and
# stops, rather than silently limping on with a broken environment.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_ROOT="$(pwd)"
STEP=0
HAD_ERROR=false

step() {
    STEP=$((STEP + 1))
    echo ""
    echo "[$STEP] $1"
    echo "----------------------------------------"
}

die() {
    echo "✗ $1"
    HAD_ERROR=true
}

ok() {
    echo "✓ $1"
}

echo "=========================================="
echo " IT Job Hunter — Setup"
echo "=========================================="

# --- 1. Operating system ---
step "Checking operating system"
OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
    ok "macOS detected ($(sw_vers -productVersion 2>/dev/null || echo unknown version))"
else
    echo "⚠ This project is designed for macOS. Detected: $OS_NAME."
    echo "  It may still work (Docker/Python/Ollama are cross-platform), but desktop"
    echo "  notifications (osascript) and some setup steps assume macOS."
fi

# --- 2. Docker ---
step "Checking Docker"
if command -v docker >/dev/null 2>&1; then
    ok "Docker CLI found ($(docker --version))"
    if docker info >/dev/null 2>&1; then
        ok "Docker daemon is running"
    else
        echo "⚠ Docker is installed but the daemon isn't running."
        echo "  Open Docker Desktop, then re-run this script (or just run: docker compose up -d)"
    fi
else
    echo "⚠ Docker not found. n8n (the workflow engine) needs Docker Desktop."
    echo "  Install: brew install --cask docker (then open it once from Applications)."
    echo "  Not required for the core pipeline/dashboard — you can skip this and come back later."
fi

# --- 3. Ollama ---
step "Checking Ollama"
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama CLI found ($(ollama --version 2>&1 | head -1))"
else
    die "Ollama not found. Install from https://ollama.com, then re-run this script."
fi

# --- 4. Python ---
step "Checking Python"
python_version_ok() {
    # True if $1 is a runnable Python 3 interpreter that's >= 3.10.
    # Version-checked rather than name-matched against a fixed list of
    # minor versions, so a newer Python (3.13, 3.14, ...) released after
    # this script was written is picked up automatically instead of the
    # script falling back to an older interpreter it happens to recognize.
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PYTHON_BIN=""
# Prefer the generic `python3` on PATH — it's whatever the user (or their
# shell setup) actually intends as the default — falling back to explicit
# version binaries only if that one isn't new enough or isn't found.
for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -n "$PYTHON_BIN" ]; then
    ok "$($PYTHON_BIN --version) found ($PYTHON_BIN)"
else
    die "Python 3.10+ not found. Install from https://python.org, then re-run this script."
fi

if [ "$HAD_ERROR" = true ]; then
    echo ""
    echo "Cannot continue — fix the ✗ item(s) above first."
    exit 1
fi

# --- 5. Directories ---
step "Creating directories"
mkdir -p data applications templates scripts workflows backups
ok "data/ applications/ templates/ scripts/ workflows/ backups/"

# --- 6. .env ---
step "Creating .env"
if [ -f .env ]; then
    ok ".env already exists — leaving it as-is"
else
    cp .env.example .env
    ok "Created .env from .env.example — edit it to customize settings"
fi

# --- Python venv + dependencies ---
step "Setting up Python virtual environment"
if [ ! -d .venv ]; then
    "$PYTHON_BIN" -m venv .venv
    ok "Created .venv"
else
    ok ".venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
if pip install --quiet -r requirements.txt; then
    ok "Python dependencies installed"
else
    die "pip install -r requirements.txt failed — see output above"
fi

# --- Playwright browser ---
step "Checking Playwright's headless Chromium"
if python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    b.close()
" >/dev/null 2>&1; then
    ok "Chromium already installed"
else
    echo "Downloading Playwright's Chromium browser (~150-180MB, one-time, from playwright.dev)..."
    if python -m playwright install chromium; then
        ok "Chromium installed"
    else
        die "Playwright Chromium install failed — PDF generation won't work until this succeeds"
    fi
fi

# --- 7/8. Start and verify n8n ---
step "Starting n8n"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if docker compose up -d; then
        ok "docker compose up -d ran"
        echo "Waiting for n8n to become reachable..."
        N8N_URL="http://localhost:${N8N_PORT:-5678}"
        N8N_UP=false
        for _ in $(seq 1 15); do
            if curl -s -o /dev/null --max-time 2 "$N8N_URL" 2>/dev/null; then
                N8N_UP=true
                break
            fi
            sleep 2
        done
        if [ "$N8N_UP" = true ]; then
            ok "n8n reachable at $N8N_URL"
        else
            echo "⚠ n8n didn't respond within 30s — check: docker compose logs n8n"
        fi
    else
        echo "⚠ docker compose up -d failed — see output above"
    fi
else
    echo "⚠ Skipping n8n (Docker not available) — not required for the core pipeline/dashboard"
fi

# --- 9. Verify Ollama ---
step "Verifying Ollama is running"
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if curl -s -o /dev/null --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null; then
    ok "Ollama reachable at $OLLAMA_URL"
else
    echo "⚠ Ollama isn't responding at $OLLAMA_URL. Start it with: ollama serve"
fi

# --- 10. Recommended model ---
step "Checking local AI models"
PULLED_MODELS=$(curl -s --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -o '"name":"[^"]*"' | cut -d'"' -f4)
if [ -n "$PULLED_MODELS" ]; then
    ok "Models already pulled:"
    echo "$PULLED_MODELS" | sed 's/^/    - /'
    echo "  Run 'python -m src.ai.benchmark' any time to re-benchmark and pick the best one for this machine."
else
    echo "No models pulled yet. Pulling the recommended default (llama3) — this may take a few minutes..."
    if ollama pull llama3; then
        ok "llama3 pulled"
    else
        echo "⚠ ollama pull llama3 failed — pull a model manually before running the pipeline."
    fi
fi

# --- 11. Initialize SQLite ---
step "Initializing database"
if python -c "from src.database.db import init_db; init_db()"; then
    ok "Database initialized at ${DATABASE_PATH:-./data/jobs.db}"
else
    die "Database initialization failed — see output above"
fi

# --- 12. Run tests ---
step "Running test suite"
if python -m pytest tests/ -q; then
    ok "All tests passed"
else
    echo "⚠ Some tests failed — see output above. Setup will continue, but investigate before relying on the system."
fi

# --- 13. Print URLs ---
step "Setup summary"
echo ""
echo "Local URLs:"
echo "  n8n:       http://localhost:${N8N_PORT:-5678}  (create your owner account on first visit)"
echo "  Dashboard: http://localhost:${DASHBOARD_PORT:-8420}  (start with: python -m src.dashboard.app)"
echo "  Ollama:    ${OLLAMA_BASE_URL:-http://localhost:11434}"
echo ""
if [ ! -f data/master_resume.json ]; then
    echo "⚠ data/master_resume.json doesn't exist yet — the system can't tailor resumes or write"
    echo "  cover letters without it. See templates/master_resume.example.json for the structure."
    echo ""
fi
echo "Next steps:"
echo "  1. source .venv/bin/activate"
echo "  2. python -m src.dashboard.app     # start the dashboard"
echo "  3. Visit http://localhost:${DASHBOARD_PORT:-8420} and use 'Import job' to add your first job"
echo ""
echo "Run scripts/healthcheck.sh any time to verify everything is still working."
echo "Run scripts/backup.sh to back up your database, resume, and generated applications."
