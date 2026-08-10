#!/usr/bin/env bash
# NAS backup wrapper for the PASay-PM cron.
# Success -> prints NOTHING (so no_agent cron stays silent).
# Failure -> prints error to stderr + exits non-zero (cron alerts).
set +e
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
cd /Users/jhackuy/Documents/Codex/pasay-pm || { echo "BACKUP: cannot cd to project" >&2; exit 1; }

# read DB creds from .env
U=$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2)
P=$(grep -m1 '^POSTGRES_PASSWORD=' .env | cut -d= -f2)
D=$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2)
[ -z "$U" ] || [ -z "$P" ] && { echo "BACKUP: missing DB creds" >&2; exit 1; }

# run backup (ssh+cat streaming to Synology NAS), keep 30, fail-fast
DATABASE_URL="postgresql+psycopg2://$U:$P@localhost:5432/$D" \
BACKUP_KEEP=30 \
BACKUP_DIR=/Users/jhackuy/Documents/Codex/pasay-pm/backups \
BACKUP_REMOTE="root@192.168.50.27:/volume1/backup/pasay-pm/" \
SSH_KEY="/Users/jhackuy/.ssh/pmp_pasay_backup" \
bash scripts/backup.sh >/tmp/pasay_backup.log 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  echo "PASay-PM 数据库备份失败 (exit $rc):" >&2
  tail -8 /tmp/pasay_backup.log >&2
  exit 1
fi
# success => silent (no message)
exit 0
