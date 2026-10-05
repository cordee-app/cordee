#!/usr/bin/env bash
# backup.sh — daily safety snapshot of all Cordée databases.
#
# Backs up, under <PROJECTS_ROOT>/<name>/:
#   project.db -> project.db.bak-<YYYYMMDD>     (next to the live file)
# and under ${AINGEL_BACKUP_DIR:-/var/backups/cordee}:
#   aingel.db   -> aingel.db.bak-<YYYYMMDD>     (outside the code checkout)
#
# Method: sqlite3 online `.backup` (WAL-safe, atomic), NOT raw `cp`. All DBs
# run in WAL mode; a raw cp of the main file alone can silently miss
# uncheckpointed commits sitting in the -wal sidecar (PRAGMA quick_check
# would still pass on the stale copy).
#
# Retention: keeps the newest KEEP_BAKS dated copies per DB, deletes older.
# Integrity: PRAGMA quick_check on every fresh copy; failures go to the log.
# Requires: sqlite3 (and rclone for the optional off-host copy).

set -uo pipefail
KEEP_BAKS="${KEEP_BAKS:-8}"
# Install dir = two levels up from ops/examples/, unless CORDEE_HOME is set.
AINGEL_ROOT="${CORDEE_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}"
PROJECTS_ROOT="${AINGEL_PROJECTS_ROOT:-${AINGEL_ROOT}/projects}"
# Central DB snapshots live outside the code checkout so they never clutter the
# repo root. Per-project snapshots stay next to their project.db on purpose.
BACKUP_DIR="${AINGEL_BACKUP_DIR:-/var/backups/cordee}"
STAMP="$(date +%Y%m%d)"
FAILED=0

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# DB snapshots hold every tenant's data (users, tasks, chats): owner-only.
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

backup_one() {
    local src="$1" dest_dir="$2" base="$3"
    local dest="${dest_dir}/${base}.bak-${STAMP}"
    [ -f "$src" ] || { log "SKIP (missing): $src"; return 0; }
    if [ -f "$dest" ]; then
        log "SKIP (exists): $dest"
    else
        # WAL-safe online backup: includes uncheckpointed WAL frames and is
        # atomic w.r.t. concurrent readers/writers. A raw `cp` of the main
        # file alone can silently miss recent commits in -wal (quick_check
        # would still pass on the stale copy).
        sqlite3 "$src" ".backup '$dest'" || { log "FAIL backup: $src"; FAILED=1; return; }
        # Store cold copies in rollback-journal mode: self-contained file,
        # no -wal/-shm sidecars accumulating next to the .bak files.
        sqlite3 "$dest" "PRAGMA journal_mode=DELETE;" >/dev/null || true
        chown --reference="$src" "$dest" 2>/dev/null || true
        chmod --reference="$src" "$dest" 2>/dev/null || true
        local check
        check=$(sqlite3 "file:$dest?mode=ro" "PRAGMA quick_check;" 2>&1 | head -1)
        if [ "$check" = "ok" ]; then
            log "OK: $dest ($(du -h "$dest" | cut -f1))"
        else
            log "INTEGRITY FAIL: $dest — $check"; FAILED=1
        fi
    fi
}

# Retention — rotate ONLY the dated automated snapshots (bak-YYYYMMDD).
# Incident-labelled backups (bak-<label>-<date>, .premerge-*.bak) are
# historical evidence and are NEVER touched by this loop.
# The glob is anchored to the 8-digit dated form; deduped per unique prefix.
rotate_dated() {
    local dir="$1" base="$2" f prefix
    local -A prefixes=()
    for f in "${dir}/${base}".bak-*; do
        [ -e "$f" ] || continue
        case "$f" in
            *.bak-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) prefixes["${f%bak-[0-9]*}bak-"]=1 ;;
        esac
    done
    for prefix in "${!prefixes[@]}"; do
        # newest first; keep KEEP_BAKS dated copies, remove older ones only
        while read -r old; do
            [ -n "$old" ] || continue
            log "retention: rm $old"
            rm -f "$old"
        done < <(ls -1t "${prefix}"[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9] 2>/dev/null | tail -n +"$((KEEP_BAKS + 1))")
    done
}

# central DB
backup_one "${AINGEL_ROOT}/aingel.db" "${BACKUP_DIR}" "aingel.db"
rotate_dated "${BACKUP_DIR}" "aingel.db"

# per-project DBs
for d in "${PROJECTS_ROOT}"/*/; do
    name="$(basename "$d")"
    backup_one "${d}project.db" "${d%/}" "project.db"
    rotate_dated "${d%/}" "project.db"
done

# ── Off-host copy (optional) ─────────────────────────────────────────────────
# Everything above lives on the same disk as the source. Set
# AINGEL_BACKUP_RCLONE_REMOTE (e.g. "s3:cordee-backups") in .env to
# push today's snapshot to object storage with rclone. No-op (logged) when the
# remote is unset or rclone is not installed, so the local backup still runs.
if [ -n "${AINGEL_BACKUP_RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null 2>&1; then
    RCLONE_LOG="${BACKUP_DIR}/backup-rclone.log"
    log "off-host: rclone → ${AINGEL_BACKUP_RCLONE_REMOTE}"
    if [ -f "${BACKUP_DIR}/aingel.db.bak-${STAMP}" ]; then
        rclone copy "${BACKUP_DIR}/aingel.db.bak-${STAMP}" \
            "${AINGEL_BACKUP_RCLONE_REMOTE}/central/" 2>>"$RCLONE_LOG" \
            || { log "off-host FAIL: central (see $RCLONE_LOG)"; FAILED=1; }
    fi
    for d in "${PROJECTS_ROOT}"/*/; do
        name="$(basename "$d")"
        src="${d}project.db.bak-${STAMP}"
        [ -f "$src" ] || continue
        rclone copy "$src" "${AINGEL_BACKUP_RCLONE_REMOTE}/projects/${name}/" \
            2>>"$RCLONE_LOG" || { log "off-host FAIL: ${name}"; FAILED=1; }
    done
    log "off-host: done"
else
    log "off-host: SKIP (set AINGEL_BACKUP_RCLONE_REMOTE and install rclone to enable)"
fi

log "done (FAILED=${FAILED})"
exit $FAILED