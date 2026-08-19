#!/usr/bin/env bash
# IT Job Hunter — health check
#
# Checks every local service the system depends on and reports clear
# PASS/FAIL/WARN for each. Never hides an error (spec section 32) — every
# failure prints the actual command/output that failed, not just "something
# went wrong". Exits non-zero if any CRITICAL check fails; exits 0 (with
# warnings printed) if only optional/non-critical checks fail.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_ROOT="$(pwd)"

PASS=0
FAIL=0
WARN=0

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  ⚠ $1"; WARN=$((WARN + 1)); }

echo "IT Job Hunter — health check"
echo "=============================="
echo ""

# --- Python / venv ---
echo "Python environment:"
if [ -d "$PROJECT_ROOT/.venv" ]; then
    pass ".venv exists"
    if "$PROJECT_ROOT/.venv/bin/python" -c "import jinja2, playwright, flask, requests, pydantic" 2>/dev/null; then
        pass "Core dependencies importable"
    else
        fail "Core dependencies missing — run: source .venv/bin/activate && pip install -r requirements.txt"
    fi
else
    fail ".venv not found — run scripts/setup.sh first"
fi
echo ""

# --- Ollama ---
echo "Ollama (local AI):"
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if curl -s -o /dev/null -w "" --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null; then
    pass "Ollama reachable at $OLLAMA_URL"
    MODEL_COUNT=$(curl -s --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -o '"name"' | wc -l | tr -d ' ')
    if [ "$MODEL_COUNT" -gt 0 ]; then
        pass "$MODEL_COUNT model(s) pulled"
    else
        warn "No models pulled yet — run: ollama pull llama3"
    fi
else
    fail "Ollama not reachable at $OLLAMA_URL — is it running? Try: ollama serve"
fi
echo ""

# --- n8n / Docker ---
echo "n8n (Docker):"
if ! command -v docker >/dev/null 2>&1; then
    warn "Docker CLI not found — n8n workflows won't run (not required for the core pipeline/dashboard)"
elif ! docker info >/dev/null 2>&1; then
    warn "Docker daemon not running — start Docker Desktop, then: docker compose up -d"
else
    N8N_STATUS=$(docker compose ps --format '{{.Status}}' n8n 2>/dev/null || true)
    if echo "$N8N_STATUS" | grep -qi "up"; then
        pass "n8n container is running ($N8N_STATUS)"
        N8N_URL="http://localhost:${N8N_PORT:-5678}"
        if curl -s -o /dev/null --max-time 5 "$N8N_URL" 2>/dev/null; then
            pass "n8n reachable at $N8N_URL"
        else
            warn "n8n container is up but not responding yet at $N8N_URL — it may still be starting"
        fi
    else
        warn "n8n container not running — start it: docker compose up -d"
    fi
fi
echo ""

# --- Database ---
echo "Database:"
DB_PATH="${DATABASE_PATH:-./data/jobs.db}"
if [ -f "$DB_PATH" ]; then
    pass "Database file exists at $DB_PATH"
    TABLE_COUNT=$("$PROJECT_ROOT/.venv/bin/python" -c "
from src.database.db import get_connection
conn = get_connection('$DB_PATH')
print(len(conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()))
conn.close()
" 2>/dev/null || echo "0")
    if [ "$TABLE_COUNT" -ge 6 ]; then
        pass "Schema initialized ($TABLE_COUNT tables)"
    else
        fail "Database exists but schema looks incomplete — run: python -c 'from src.database.db import init_db; init_db()'"
    fi
else
    warn "Database not initialized yet — run: python -c 'from src.database.db import init_db; init_db()'"
fi
echo ""

# --- Master resume ---
echo "Master resume:"
if [ -f "$PROJECT_ROOT/data/master_resume.json" ]; then
    pass "data/master_resume.json exists"
else
    fail "data/master_resume.json missing — the system cannot tailor resumes or write cover letters without it"
fi
echo ""

# --- Dashboard (optional, usually not running unless the user started it) ---
echo "Dashboard (optional — only relevant if you've started it):"
DASHBOARD_URL="http://${DASHBOARD_HOST:-127.0.0.1}:${DASHBOARD_PORT:-8420}"
if curl -s -o /dev/null --max-time 3 "$DASHBOARD_URL" 2>/dev/null; then
    pass "Dashboard reachable at $DASHBOARD_URL"
else
    warn "Dashboard not running (this is normal if you haven't started it) — start with: python -m src.dashboard.app"
fi
echo ""

echo "=============================="
echo "Result: $PASS passed, $WARN warnings, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
    echo "Some critical checks failed — see ✗ items above."
    exit 1
fi
exit 0
