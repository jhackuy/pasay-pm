#!/usr/bin/env bash
#
# PASay-PM PostgreSQL backup -> NAS
#
#  1. pg_dump (custom format) -> gzip -> timestamped dump
#  2. stream over SSH (`ssh 'cat > file'`) to the NAS
#     (The NAS box here is a Synology which restricts the `rsync --server`
#      / `sftp` subsystems for root, so we use the plain-auth SSH stream.)
#  3. prunes the newest BACKUP_KEEP backups locally and on the NAS
#
# All settings can be overridden via environment variables:
#   BACKUP_REMOTE  remote "user@host:/path/"  (or a local dir for testing)
#   BACKUP_KEEP    how many backups to keep (default: 30)
#   DATABASE_URL   SQLAlchemy-style URL (default: local .env value)
#   SSH_KEY        SSH private key for the NAS (default: ~/.ssh/pmp_pasay_backup)
#   BACKUP_DIR     local staging directory (default: ./backups)
#   SSH_PORT       SSH port (default: 22)
#
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-backups}"
BACKUP_REMOTE="${BACKUP_REMOTE:-root@192.168.50.27:/volume1/backup/pasay-pm/}"
BACKUP_KEEP="${BACKUP_KEEP:-30}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/pmp_pasay_backup}"
SSH_PORT="${SSH_PORT:-22}"
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm}"

# --- parse DATABASE_URL: postgresql+psycopg2://user:pass@host:port/dbname ---
url="${DATABASE_URL#*://}"
creds="${url%%@*}"
hostport_db="${url#*@}"
dbname="${hostport_db##*/}"
hostport="${hostport_db%%/*}"
host="${hostport%%:*}"
port="${hostport#*:}"
[ -n "$port" ] || port=5432
user="${creds%%:*}"
password="${creds#*:}"

export PGPASSWORD="$password"

PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"
if command -v "$PG_DUMP_BIN" >/dev/null 2>&1; then
    pg_dump_cmd=("$PG_DUMP_BIN")
else
    echo "!! pg_dump not found on host; falling back to 'docker compose exec -T db pg_dump'"
    pg_dump_cmd=(docker compose exec -T db pg_dump)
fi

ts="$(date +%Y%m%d_%H%M%S)"
dump_file="${BACKUP_DIR}/pasay_pm_${ts}.dump.gz"
keep=$((BACKUP_KEEP + 1))

echo "==> pg_dump '${dbname}' @ ${host}:${port} (user ${user})"
mkdir -p "${BACKUP_DIR}"
"${pg_dump_cmd[@]}" -h "$host" -p "$port" -U "$user" -d "$dbname" -Fc | gzip > "$dump_file"
echo "==> Created ${dump_file} ($(du -h "$dump_file" | cut -f1))"

# --- push to NAS (or a local dir for testing) ---
if [[ "$BACKUP_REMOTE" == *:* ]]; then
    remote_host="${BACKUP_REMOTE%%:*}"
    remote_path="${BACKUP_REMOTE#*:}"
    # ensure the remote directory exists
    ssh -p "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes "$remote_host" "mkdir -p '${remote_path%/}'"
    # stream the gzipped dump over SSH; write it atomically on the NAS
    echo "==> streaming -> ${remote_host}:${remote_path}"
    cat "$dump_file" | ssh -p "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes \
        "$remote_host" "cat > '${remote_path%/}/$(basename "$dump_file")'"
    echo "==> pruning remote backups older than the newest ${BACKUP_KEEP}"
    ssh -p "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes "$remote_host" \
        "cd '${remote_path%/}' && ls -1t pasay_pm_*.dump.gz 2>/dev/null | tail -n +${keep} | xargs -r rm -f"
else
    mkdir -p "${BACKUP_REMOTE%/}"
    cp "$dump_file" "${BACKUP_REMOTE%/}/"
    echo "==> pruning local backup dir older than the newest ${BACKUP_KEEP}"
    ls -1t "${BACKUP_REMOTE%/}"/pasay_pm_*.dump.gz 2>/dev/null \
        | tail -n +"${keep}" | xargs -r rm -f
fi

# --- local retention ---
echo "==> pruning ${BACKUP_DIR} older than the newest ${BACKUP_KEEP}"
ls -1t "${BACKUP_DIR}"/pasay_pm_*.dump.gz 2>/dev/null \
    | tail -n +"${keep}" | xargs -r rm -f

echo "==> Backup complete"
