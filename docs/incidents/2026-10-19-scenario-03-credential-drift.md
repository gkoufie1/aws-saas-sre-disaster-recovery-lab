# Post-incident review: database password rotation became an outage at the next DB restart

> Lab scenario 03, injected on purpose. Local kind cluster, Kubernetes 1.35, API 1.0.1. All times UTC.

| | |
|---|---|
| **Date** | 2026-10-19 |
| **Severity** | SEV1. Full API outage: every route, reads and writes |
| **Trigger** | DB password rotated in PostgreSQL but not in the Kubernetes Secret; later, a routine DB restart |
| **Latent period** | 60 s with **zero** impact after the rotation (pooled connections stayed authenticated) |
| **Duration of impact** | 19:57:41 → 20:00:56 (**3 min 15 s**) |
| **Time to detect (TTD)** | **60 s** after impact (`APIAvailabilityFastBurn`) |
| **Time to mitigate** | 4 s from fix to last failed request; 16 s to confirmed recovery |
| **Error budget consumed** | 551 failed client requests = **2.13%** of the 30-day availability budget |
| **Side effect** | The backup at 20:00 **failed**: backups use the same Secret |
| **Evidence** | `evidence/scenario-03-20261019T195640Z.json` |

## Summary

Someone rotated the application's database password directly in PostgreSQL. The Kubernetes Secret
still held the old value. Nothing broke, because the API's connection pool held connections that had
authenticated before the change. A minute later PostgreSQL restarted (routine maintenance). Every
connection dropped, every reconnect failed authentication, and the whole API went down. The scheduled
backup failed the same way.

## Timeline

| Time | Offset | Event |
|---|---|---|
| 19:56:40 | T+0 s | `ALTER ROLE app WITH PASSWORD '<new>'` in PostgreSQL only |
| 19:56:40–19:57:40 | | **No impact.** Probe 100% successful on pooled connections |
| 19:57:40 | T+60 s | PostgreSQL pod restarted, dropping all connections |
| 19:57:41 | T+61 s | First failed request |
| 19:58:40 | T+121 s | **Page: `APIAvailabilityFastBurn`** |
| 19:58:41+ | | PostgreSQL logs `FATAL: password authentication failed for user "app"` |
| 19:59:21 | T+162 s | `APIDatabaseErrors` |
| 20:00:06 | T+207 s | `SyntheticClientFailing` |
| 20:00:21 | T+222 s | `APIReplicasUnavailable` (readiness failing on both pods) |
| 20:00:36 | T+237 s | `PostgresBackupJobFailed` (the 20:00 backup could not authenticate) |
| 20:00:52 | T+252 s | **Roll forward:** Secret updated to the rotated password; `rollout restart` of the API |
| 20:00:56 | T+256 s | Last failed request |
| 20:01:08 | T+268 s | Recovered |

## Diagnosis

The answer came from the **server's** log, not the client's:

```text
# API (client side): not specific
db unavailable op=create err=PoolTimeout: couldn't get a connection after 3.00 sec
error connecting in 'pool-1': ... FATAL:  password authentication failed for user "app"

# PostgreSQL (server side): exact
FATAL:  password authentication failed for user "app"
DETAIL: Connection matched file ".../pg_hba.conf" line 128: "host all all all scram-sha-256"
```

The runbook's layer-by-layer order (DB process up → Service routes → app error → server error) took
four commands to reach this.

## Hypothesis vs. what happened

| Hypothesis | Result |
|---|---|
| Nothing breaks until connections drop | ✅ Confirmed: 60 s latent, zero errors |
| After the DB restart, every API pod fails readiness → full outage | ✅ Confirmed |
| Server-side SLI "goes blind" | ❌ **Wrong.** The API kept running and answered 503s, so server-side 5xx metrics existed and the burn-rate alert paged *first*, 86 s before the black-box alert. Blindness needs the *process* to be gone (scenario 01) |
| Backups also fail | ✅ Confirmed |

## Root cause and contributing factors

- **Root cause:** the credential has two copies (database and Secret) and they were changed separately.
- **The failure was latent.** Rotation "worked", and the break surfaced at an unrelated later event.
  Without the restart it could have stayed hidden until the next deploy, possibly days later, with the
  link to the rotation lost.
- **The API failed slowly.** Each request waited the full 3 s pool-acquisition timeout before
  returning 503: API p95 was **4.7 s** and clients timed out at 2 s. Fast failure would have meant
  clearer client errors and less resource pile-up.
- **Shared fate:** the backup job reads the same Secret, so the outage also stopped backups and put
  the RPO at risk at the same moment.

## What went well

- Roll-forward (update the Secret to match) was the right call: the rotation was intended, and
  reverting would have undone a security action.
- Recovery took 16 s once the Secret was right.

## Action items

| Action | Type | Status |
|---|---|---|
| Single source of truth: on AWS, RDS-managed password in Secrets Manager, delivered by External Secrets Operator / CSI driver with rotation | prevent | Designed (`terraform/rds.tf` `manage_master_user_password`); delivery to pods open |
| Rotation procedure: change → **test a fresh connection** → only then consider it done | prevent | Documented in [runbook](../runbooks/database-connectivity.md) §3 |
| Alert on authentication failures specifically (PostgreSQL log / `pg_stat_database` errors) | detect | Open |
| Lower the pool-acquisition timeout on the request path (3 s → ~1 s) so outages fail fast | mitigate | Open |
| `PostgresBackupStale` threshold 15 min → 7 min (it was 3× the RPO target) | detect | **Done** |
| **Found later:** the failed backup left 0-byte `.dump` objects that a restore would have picked as "newest" ([DR Drill 001](../dr/dr-drill-001-results.md)). Backups are now validated before upload, and restores skip invalid archives | prevent | **Done**, regression-tested |
| `PostgresBackupJobFailed` kept firing an hour after backups recovered (failed Job objects persist). Rewritten to count only jobs started in the last 15 min | detect | **Done** |

## Change-management note

The trigger was an **out-of-band** change: an `ALTER ROLE` run directly against the database, with no
ticket, no review and no record outside the database. This is the change that SOC 2 / ISO 27001
change-management controls exist to stop.
