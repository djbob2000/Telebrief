#!/usr/bin/env bash
# ==============================================================================
# Telebrief - Automated Database Backup to Mega.nz via Rclone
# ==============================================================================
# Usage:
#   ./scripts/backup_to_mega.sh
#   or: make backup
#
# Required .env variables:
#   MEGA_USER=your_email@example.com
#   MEGA_PASSWORD=your_mega_password
#
# Optional .env variables:
#   POSTGRES_USER=telebrief
#   POSTGRES_DB=telebrief
#   POSTGRES_CONTAINER=telebrief-postgres
#   MEGA_BACKUP_DIR=telebrief-backups
#   BACKUP_RETENTION_DAYS=14
#   BACKUP_LOCAL_DIR=./data/backups
#   TELEGRAM_BOT_TOKEN=...
#   BACKUP_ALERT_CHAT_ID=...
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 1. Load environment variables from .env
ENV_FILE="${PROJECT_ROOT}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source <(grep -v '^\s*#' "$ENV_FILE" | grep -v '^\s*$')
    set +a
fi

# Check required credentials
if [ -z "${MEGA_USER:-}" ] || [ -z "${MEGA_PASSWORD:-}" ]; then
    echo "❌ Error: MEGA_USER and MEGA_PASSWORD must be set in .env" >&2
    exit 1
fi

# Check for rclone binary
if ! command -v rclone &> /dev/null; then
    echo "❌ Error: 'rclone' is not installed." >&2
    echo "Install it with: curl https://rclone.org/install.sh | sudo bash" >&2
    exit 1
fi

# Default configuration values
POSTGRES_USER="${POSTGRES_USER:-telebrief}"
POSTGRES_DB="${POSTGRES_DB:-telebrief}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-telebrief-postgres}"
MEGA_BACKUP_DIR="${MEGA_BACKUP_DIR:-telebrief-backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-${PROJECT_ROOT}/data/backups}"

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
FILENAME="telebrief_db_${TIMESTAMP}.dump"
LOCAL_PATH="${BACKUP_LOCAL_DIR}/${FILENAME}"

mkdir -p "$BACKUP_LOCAL_DIR"

# Optional Telegram notification helper
send_tg_alert() {
    local message="$1"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${BACKUP_ALERT_CHAT_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${BACKUP_ALERT_CHAT_ID}" \
            -d "text=${message}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

echo "=================================================="
echo "🚀 Starting Telebrief Database Backup: ${TIMESTAMP}"
echo "=================================================="

# 2. Create PostgreSQL dump with zstd:7 compression
echo "📦 [1/3] Dumping database '${POSTGRES_DB}' (format: custom, compression: zstd:7)..."

START_TIME=$(date +%s)

docker exec -i "${POSTGRES_CONTAINER}" pg_dump \
    -U "${POSTGRES_USER}" \
    -Fc \
    -Z zstd:7 \
    "${POSTGRES_DB}" > "${LOCAL_PATH}"

FILESIZE_HUMAN=$(du -h "${LOCAL_PATH}" | cut -f1)
echo "✅ Dump created successfully: ${LOCAL_PATH} (${FILESIZE_HUMAN})"

# 3. Configure on-the-fly rclone backend for Mega.nz
echo "☁️  [2/3] Uploading dump to Mega.nz (${MEGA_BACKUP_DIR}/)..."

OBSCURED_PASS=$(rclone obscure "${MEGA_PASSWORD}")
export RCLONE_CONFIG_MEGABACKUP_TYPE="mega"
export RCLONE_CONFIG_MEGABACKUP_USER="${MEGA_USER}"
export RCLONE_CONFIG_MEGABACKUP_PASS="${OBSCURED_PASS}"

rclone copy "${LOCAL_PATH}" "MEGABACKUP:${MEGA_BACKUP_DIR}" \
    --retries 3 \
    --low-level-retries 10 \
    --stats 5s

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo "✅ Dump uploaded successfully to Mega.nz"

# 4. Retention cleanup: remove old backups in Mega and locally
echo "🧹 [3/3] Cleaning up backups older than ${BACKUP_RETENTION_DAYS} days..."

# Delete in Mega cloud
rclone delete --min-age "${BACKUP_RETENTION_DAYS}d" "MEGABACKUP:${MEGA_BACKUP_DIR}" 2>/dev/null || true

# Delete local copies
find "${BACKUP_LOCAL_DIR}" -name "telebrief_db_*.dump" -type f -mtime +"${BACKUP_RETENTION_DAYS}" -delete 2>/dev/null || true

echo "=================================================="
echo "🎉 Backup completed in ${DURATION}s!"
echo "File: ${FILENAME} (${FILESIZE_HUMAN})"
echo "=================================================="

# Send status notification to Telegram (if configured)
send_tg_alert "🟢 <b>Telebrief Backup: Success</b>%0A📦 File: <code>${FILENAME}</code>%0A📊 Size: <b>${FILESIZE_HUMAN}</b> (zstd:7)%0A⏱ Duration: <b>${DURATION}s</b>%0A☁️ Storage: Mega.nz/${MEGA_BACKUP_DIR}"
