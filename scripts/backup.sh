#!/usr/bin/env bash
# IT Job Hunter — local backup (spec section 31)
#
# Backs up: database, master resume, application files, settings, n8n
# workflow exports. Does NOT back up secrets — .env is deliberately
# excluded (it may hold TELEGRAM_BOT_TOKEN, N8N_BASIC_AUTH_PASSWORD, etc.);
# re-create it from .env.example after a restore instead.
#
# Output: backups/job-hunter-backup-YYYYMMDD-HHMMSS.tar.gz (backups/ is
# gitignored — this script never pushes a backup anywhere, it's local-only
# just like everything else in this project).
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_ROOT="$(pwd)"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$PROJECT_ROOT/backups"
STAGING_DIR="$(mktemp -d)"
ARCHIVE_NAME="job-hunter-backup-$TIMESTAMP.tar.gz"

trap 'rm -rf "$STAGING_DIR"' EXIT

mkdir -p "$BACKUP_DIR"

echo "IT Job Hunter — backup"
echo "======================="
echo ""

copied_any=false

backup_item() {
    local src="$1"
    local label="$2"
    if [ -e "$src" ]; then
        mkdir -p "$STAGING_DIR/$(dirname "$label")"
        cp -R "$src" "$STAGING_DIR/$label"
        echo "  + $label"
        copied_any=true
    else
        echo "  - $label (not present, skipped)"
    fi
}

echo "Collecting:"
backup_item "data/jobs.db" "data/jobs.db"
backup_item "data/master_resume.json" "data/master_resume.json"
backup_item "data/settings.json" "data/settings.json"
backup_item "applications" "applications"
backup_item "workflows" "workflows"
echo ""

if [ "$copied_any" = false ]; then
    echo "Nothing found to back up — is this run from the project root with data already initialized?"
    exit 1
fi

tar -czf "$BACKUP_DIR/$ARCHIVE_NAME" -C "$STAGING_DIR" .

SIZE=$(du -h "$BACKUP_DIR/$ARCHIVE_NAME" | cut -f1)
echo "Backup created: backups/$ARCHIVE_NAME ($SIZE)"
echo ""
echo "Note: .env was intentionally NOT included (it may hold secrets)."
echo "To restore: tar -xzf backups/$ARCHIVE_NAME -C /path/to/restored/project"

# Keep only the 10 most recent backups so this doesn't grow unbounded.
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/job-hunter-backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt 10 ]; then
    echo ""
    echo "Pruning old backups (keeping the 10 most recent)..."
    ls -1t "$BACKUP_DIR"/job-hunter-backup-*.tar.gz | tail -n +11 | xargs rm -f
fi
