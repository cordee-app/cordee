# Restoring Cordée databases

`ops/examples/backup.sh` (run daily by `cordee-db-backup.timer`) writes
WAL-safe `sqlite3 .backup` copies, each checked with `PRAGMA quick_check`:

- Central DB: `$AINGEL_BACKUP_DIR/aingel.db.bak-YYYYMMDD`
  (default `/var/backups/cordee/`).
- Project DBs: `<AINGEL_PROJECTS_ROOT>/<project>/project.db.bak-YYYYMMDD`.
- Off-host copies, when `AINGEL_BACKUP_RCLONE_REMOTE` is set: the day's
  snapshots under `central/` and `projects/<name>/` on that remote.

The paths below assume the example install (`/opt/cordee`, projects in
`/opt/cordee/projects`). Adjust them to your `.env`.

## Restore a project DB

1. Stop the service. Restoring while it runs is not safe:
   `sudo systemctl stop cordee`
2. Keep the current file, then copy the snapshot over it:
   ```bash
   cd /opt/cordee/projects/<project>
   cp project.db project.db.pre-restore-$(date +%Y%m%d%H%M)
   cp project.db.bak-YYYYMMDD project.db
   rm -f project.db-wal project.db-shm   # stale WAL from the old file
   ```
3. Check it: `sqlite3 project.db 'PRAGMA quick_check;'` should print `ok`.
4. Start the service: `sudo systemctl start cordee`

## Restore the central DB

Same steps for `/opt/cordee/aingel.db`, copying from `$AINGEL_BACKUP_DIR`.
The central DB holds the project, task and user registries, so restoring it
without the matching project DBs can leave dangling ids. Prefer restoring both
from the same day.

## From off-host storage

```bash
rclone copy <remote>/central/aingel.db.bak-YYYYMMDD /tmp/restore/
rclone copy <remote>/projects/<project>/project.db.bak-YYYYMMDD /tmp/restore/
```

Then follow the steps above with the downloaded files.

## Check a backup before trusting it

```bash
sqlite3 'file:aingel.db.bak-YYYYMMDD?mode=ro' 'PRAGMA quick_check;'
```
