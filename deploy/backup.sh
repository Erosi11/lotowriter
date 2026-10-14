#!/usr/bin/env bash
# Nightly backup of LotoWriter: database (safe copy of the live file) + photos + generated documents.
# Snapshots go to BACKUP_DEST (another machine's share or a separate disk), one folder per run.
# Unchanged files are hard-linked to the previous snapshot, so each night only uses space for new files.
set -euo pipefail

DATA="${LOTOWRITER_DATA:-/var/lib/lotowriter}"
DEST="${BACKUP_DEST:?Set BACKUP_DEST, e.g. /mnt/backup/lotowriter}"
KEEP="${BACKUP_KEEP:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAP="$DEST/$STAMP"

mkdir -p "$SNAP/db"

# 1. Database: SQLite's backup API gives a consistent copy even while the app is running.
"${PYTHON:-/opt/lotowriter/.venv/bin/python}" - "$DATA/db/lotowriter.sqlite3" "$SNAP/db/lotowriter.sqlite3" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
src.close(); dst.close()
if ok != "ok":
    sys.exit(f"backup integrity check failed: {ok}")
PY

# 2. Files (photos, generated documents).
LINK=()
[ -d "$DEST/latest" ] && LINK=(--link-dest="$DEST/latest/")
rsync -a "${LINK[@]}" --exclude 'db/' --exclude 'lo-profile/' "$DATA/" "$SNAP/"

ln -sfn "$SNAP" "$DEST/latest"

# 3. Keep the newest $KEEP snapshots.
ls -1d "$DEST"/20*-* 2>/dev/null | sort | head -n -"$KEEP" | xargs -r rm -rf

# 4. Disk space warning (data disk and backup disk).
for d in "$DATA" "$DEST"; do
  used=$(df --output=pcent "$d" | tail -1 | tr -dc '0-9')
  if [ "$used" -ge 80 ]; then
    echo "WARNING: $d is ${used}% full" >&2
    logger -t lotowriter-backup "WARNING: $d is ${used}% full"
  fi
done

echo "Backup complete: $SNAP"
