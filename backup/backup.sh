#!/bin/sh
# Logical backup of the customer database to S3-compatible object storage.
# Locally the bucket lives in SeaweedFS *outside* the Kubernetes cluster, so
# backups survive losing the database volume or the whole cluster. On AWS
# the same script targets a real S3 bucket (S3_ENDPOINT unset).
#
# Env: PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD BACKUP_BUCKET [S3_ENDPOINT]
#
# The dump is written locally and validated BEFORE upload. The first version
# streamed `pg_dump | aws s3 cp -`; when pg_dump failed (scenario 03, bad
# password) the upload still completed and left a 0-byte ".dump" as the
# newest backup in the bucket. Only a verified archive is uploaded now.
set -eu

endpoint=""
[ -n "${S3_ENDPOINT:-}" ] && endpoint="--endpoint-url ${S3_ENDPOINT}"

ts=$(date -u +%Y%m%dT%H%M%SZ)
key="postgres/${PGDATABASE}/${ts}.dump"
tmp=$(mktemp /tmp/backup.XXXXXX)
trap 'rm -f "$tmp"' EXIT
started=$(date +%s)

fail() {
  printf '{"event":"backup_failed","key":"%s","reason":"%s"}\n' "$key" "$1"
  exit 1
}

# Custom format (-Fc): compressed, and pg_restore can restore it selectively.
# pg_dump takes an MVCC snapshot, so the dump is consistent as of its start.
pg_dump -Fc --no-owner --no-privileges -f "$tmp" || fail "pg_dump exited non-zero"

size=$(wc -c < "$tmp" | tr -d ' ')
[ "$size" -gt 0 ] || fail "dump is empty"
# Reading the archive's table of contents proves it is a complete pg_dump
# archive, not a truncated or corrupt file.
pg_restore -l "$tmp" > /dev/null || fail "archive failed pg_restore -l validation"

# shellcheck disable=SC2086
aws s3 cp "$tmp" "s3://${BACKUP_BUCKET}/${key}" ${endpoint} --only-show-errors \
  || fail "upload failed"

printf '{"event":"backup_complete","key":"%s","snapshot_started_utc":"%s","bytes":%s,"seconds":%s}\n' \
  "$key" "$ts" "$size" "$(( $(date +%s) - started ))"
