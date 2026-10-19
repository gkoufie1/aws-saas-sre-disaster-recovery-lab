# DR Drill 001: Database volume loss, restore from off-cluster backup

**Date:** 2026-10-19 · **Environment:** local kind cluster (Kubernetes 1.35), PostgreSQL 16, SeaweedFS (S3 API)
**Targets, set before the drill** ([runbook](../runbooks/disaster-recovery.md), [`lab.py`](../../lab.py) `cmd_dr_drill`): **RTO 15 min · RPO 5 min**

| | Target | Measured | Result |
|---|---|---|---|
| **RTO** (disaster → 10 consecutive successful write+read cycles) | 900 s | **92.2 s** | ✅ MET |
| **RPO** (disaster → newest record that survived) | 300 s | **215.7 s** | ✅ MET, **by timing, not by design**. See below |
| Records lost | — | **639** of 1,008,401 | |
| Error budget consumed | — | 124 failed client requests = **0.48%** of 30-day availability budget | |

Evidence: [`evidence/dr-drill-20261019T201334Z.json`](../../evidence/dr-drill-20261019T201334Z.json)

## What was destroyed

The PostgreSQL StatefulSet **and** its PersistentVolumeClaim, so the data files themselves were gone, not
just the process. A pod restart would not bring the data back. Only the backups in the off-cluster
object store survived.

## Timeline

| Time (UTC) | Offset | Event |
|---|---|---|
| 20:10:00 | T−216 s | Last scheduled backup snapshot starts (`20261019T201000Z.dump`, 33 MB) |
| 20:13:36.6 | **T+0** | **Disaster:** StatefulSet + PVC deleted. 1,008,401 records, newest written 20:13:36.0 |
| 20:13:37.7 | T+1.1 s | First failed request |
| 20:14:22.7 | T+46 s | Page: `APIAvailabilityFastBurn` |
| 20:14:22.8 | T+46 s | Runbook step 1: **stop writes**, API and load generator scaled to 0 |
| 20:14:23.2 | T+47 s | Step 2: recreate an empty PostgreSQL on a new volume |
| 20:14:35.6 | T+59 s | Step 3: restore Job starts, pulling the newest dump from object storage |
| 20:14:52.8 | T+76 s | Restore complete (`pg_restore` 12 s for 1M rows + indexes) |
| 20:14:53.2 | T+77 s | Step 4 validation: 1,007,762 rows; newest record **20:10:00.9** |
| 20:14:53.2 | T+77 s | Step 5: reopen, API scaled to 2 and load generator restarted |
| 20:14:57.8 | T+81 s | Last failed request |
| 20:15:08.8 | **T+92.2 s** | **Recovered:** 10 consecutive successful write+read cycles |

## Reading the numbers honestly

**RTO of 92 s is the floor, not the expectation.** Every step after the page was executed by script,
with no decision time. The 46 s before step 1 was the alert, not a human. A person who has to wake up,
open the runbook, confirm this is data loss and not an outage, and choose a restore point would add
minutes. The 15-minute target still has a wide margin, but the honest claim is *"the mechanics take
about 1.5 minutes"*, not *"we recover in 1.5 minutes"*.

**RPO met by timing.** The disaster struck 3 min 36 s after the last backup started. With backups
every 5 minutes and a ~10–15 s dump, **the worst case is about 5 min 15 s, which misses the 300 s
target.** This drill passed because it happened to land early in the cycle. The design does not
guarantee the target.

**RPO is only as good as the backups that actually succeeded.** Earlier the same afternoon, scenario 03
made the 20:00 backup fail. A disaster between 20:00 and the next good backup at 20:05 would have restored
from 19:55: a loss of **5 to 10 minutes**. At the time, `PostgresBackupStale` would not have fired until 15 minutes without a
backup (now fixed to 7 min).

**The bucket held two 0-byte "backups", and they were the newest ones.** Listing the bucket after
the drill showed `20261019T200000Z.dump` and `20261019T200013Z.dump` at **0 bytes**. These were the
two attempts of the backup that failed in scenario 03. The original `backup.sh` streamed
`pg_dump | aws s3 cp -`. When `pg_dump` failed authentication the job exited non-zero (so the alert
fired), but `aws s3 cp` had already completed an upload of the empty stream. `restore.sh` picked the
newest `.dump` by name. **A disaster between 20:00 and 20:05 would have tried to restore an empty
file**, and any check that counted objects in the bucket would have looked healthy.

Fixed and verified ([`scripts/test_backup_safety.py`](../../scripts/test_backup_safety.py)):

- `backup.sh` writes the dump locally, rejects it if it is empty or fails `pg_restore -l`, and only
  then uploads. Test: a backup forced to fail authentication logged `backup_failed` and uploaded
  **nothing**.
- `restore.sh` ignores 0-byte objects and validates each candidate newest-first. Test: with an empty
  and a corrupt object planted as the two newest backups, the restore logged
  `backup_skipped ... invalid archive` and restored the newest *valid* dump.
- New `lab.py verify-backup` restores the newest backup into a scratch database and checks rows and
  indexes. First run: **PASS**, 1,010,268 rows restored, newest record 45 s old, all 4 indexes present,
  15 s restore.

**Stopping writes first mattered.** The API runs schema migrations at startup. Restarted against the
empty database, it would have created empty tables and accepted new customers. The restore would then
have dropped those rows (`pg_restore --clean`), silently losing writes made *after* the disaster on
top of the ones lost before it.

## Verified, not assumed

- **Data:** row counts and `max(created_at)` read with `psql` **before** traffic resumed, so new
  writes could not mask the loss.
- **Schema:** the restored dump's table of contents (`pg_restore -l`) holds the table data, the
  primary-key and unique constraints, and both indexes (`customers_email_idx`,
  `customers_created_at_idx`). A restore that silently dropped the email index would recreate
  scenario 02 (a 63× latency regression) straight after recovery.
- **Backups off the blast radius:** 17 dumps in the bucket, outside the Kubernetes cluster.

## Action items

| Action | Why | Status |
|---|---|---|
| Validate before upload; restore skips invalid archives | 0-byte backups were the newest objects in the bucket | **Done**, regression test passes |
| Scheduled restore verification (`verify-backup` as a CronJob, alert on failure) | A backup is only proven by a restore | Command done; scheduling open |
| `PostgresBackupStale` 15 min → 7 min (page) | 15 min was 3× the RPO target | **Done** |
| Continuous WAL archiving (WAL-G / pgBackRest) to object storage | Turns worst-case RPO from ~5 min into seconds; the only way to *guarantee* 5 min | Open (local) |
| On AWS: RDS automated backups + PITR as the primary mechanism, measured in DR Drill 002 | `LatestRestorableTime` is typically < 5 min behind | Terraform ready, drill not yet run |
| Human-run drill (runbook only, no script), timed | Measure the realistic RTO, not the mechanical floor | Open |
| Whole-cluster loss drill: `kind delete cluster`, rebuild, restore | Proves backups are independent of the cluster, not just of the volume | Open |
