# ADR 002: Logical dumps locally; RDS PITR as the primary RPO mechanism on AWS

**Status:** Accepted · 2026-10-19

## Context

The RPO target is 5 minutes. Locally there is no managed point-in-time recovery.

## Decision

- **Local:** `pg_dump -Fc` every 5 minutes to off-cluster S3-compatible storage.
- **AWS:** RDS automated backups with transaction-log archiving (PITR) are the primary mechanism.
  The same `pg_dump` job still runs to S3 as a portable, independent copy.

## Consequences

- A 5-minute dump schedule bounds the *design* RPO at about 5 minutes **plus** dump duration, and only
  if every dump succeeds. The drill measures the real number instead of assuming it. A 5-minute schedule
  does not guarantee a 5-minute RPO.
- Continuous WAL archiving (pgBackRest / WAL-G) would bring the local RPO to seconds. It is a known
  improvement, not implemented, because on AWS the managed PITR already provides it.
- Backup freshness is monitored (`PostgresBackupStale`), because RPO silently degrades when backups stop.
