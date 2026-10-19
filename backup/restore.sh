#!/bin/sh
# Restore the newest *valid* (or a named) logical backup into the target database.
#
# Env: PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD BACKUP_BUCKET
#      [S3_ENDPOINT] [RESTORE_KEY]
#      [BACKUP_SOURCE_DB]  database whose backups to use (default: PGDATABASE).
#                          Lets a verification job restore customers' backups
#                          into a scratch database.
set -eu

endpoint=""
[ -n "${S3_ENDPOINT:-}" ] && endpoint="--endpoint-url ${S3_ENDPOINT}"
prefix="postgres/${BACKUP_SOURCE_DB:-$PGDATABASE}/"
tmp=$(mktemp /tmp/restore.XXXXXX)
trap 'rm -f "$tmp"' EXIT

# Download a key and accept it only if it is a non-empty, readable archive.
fetch_valid() {
  # shellcheck disable=SC2086
  aws s3 cp "s3://${BACKUP_BUCKET}/$1" "$tmp" ${endpoint} --only-show-errors || return 1
  [ -s "$tmp" ] || return 1
  pg_restore -l "$tmp" > /dev/null 2>&1
}

if [ -n "${RESTORE_KEY:-}" ]; then
  fetch_valid "$RESTORE_KEY" || { echo '{"event":"restore_failed","reason":"named backup is missing or invalid"}'; exit 1; }
else
  # Newest first. Keys embed a UTC timestamp, so lexical order is chronological.
  # Skip zero-byte objects up front, then validate each candidate.
  # shellcheck disable=SC2086
  candidates=$(aws s3 ls "s3://${BACKUP_BUCKET}/${prefix}" ${endpoint} \
    | awk '$3 > 0 && $4 ~ /\.dump$/ {print $4}' | sort -r)
  RESTORE_KEY=""
  for name in $candidates; do
    if fetch_valid "${prefix}${name}"; then
      RESTORE_KEY="${prefix}${name}"
      break
    fi
    printf '{"event":"backup_skipped","key":"%s","reason":"invalid archive"}\n' "${prefix}${name}"
  done
  [ -n "$RESTORE_KEY" ] || { echo '{"event":"restore_failed","reason":"no valid backups found"}'; exit 1; }
fi

until pg_isready -q; do echo '{"event":"waiting_for_database"}'; sleep 2; done

started=$(date +%s)
pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error -d "$PGDATABASE" "$tmp"

printf '{"event":"restore_complete","key":"%s","seconds":%s}\n' \
  "$RESTORE_KEY" "$(( $(date +%s) - started ))"
