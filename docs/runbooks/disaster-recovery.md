# Runbook: Database disaster recovery

**Alerts:** `SyntheticClientFailing`, `APIDatabaseErrors`, `PostgresBackupStale` / `PostgresBackupJobFailed` (RPO at risk)
**Targets (set before any drill):** RTO 15 minutes · RPO 5 minutes
**Automated drill:** `python lab.py dr-drill`. Results are in [docs/dr/](../dr/).

## 0. Declare it

Data loss is a different incident from an outage. Name an incident lead, start a timeline, and
**do not let anything write to a replacement database until the restore is complete.**

## 1. Stop writes

```bash
kubectl -n datareliability scale deployment/datareliability-api --replicas=0
kubectl -n datareliability scale deployment/loadgen --replicas=0
```

Why: the API runs schema migrations at startup. Against an empty database it would create empty
tables and accept new writes, which the restore then has to overwrite or reconcile.

## 2. Pick the restore point

```bash
aws --endpoint-url http://127.0.0.1:8333 s3 ls s3://datareliability-backups/postgres/customers/
```

Keys are `YYYYMMDDTHHMMSSZ.dump` (UTC, snapshot start). Newest is the default. If the newest dump
might contain the cause (for example a bad migration), choose the one before it with `RESTORE_KEY`.

**Check the sizes.** A 0-byte object is not a backup. `restore.sh` now skips empty and invalid
archives on its own and logs `backup_skipped`, but that protection only exists because a failed backup
once left 0-byte files as the newest objects ([DR Drill 001](../dr/dr-drill-001-results.md)).
`python lab.py verify-backup` proves the newest backup restores, without touching production.

## 3. Recreate the database and restore

```bash
kubectl apply -f k8s/postgres.yaml
kubectl -n datareliability rollout status statefulset/postgres
# restore Job: see cmd_dr_drill() in lab.py for the exact manifest; runs backup/restore.sh
```

## 4. Validate the data before reopening

```sql
SELECT count(*), max(id), max(created_at) FROM customers;
SELECT indexname FROM pg_indexes WHERE tablename = 'customers';  -- indexes restored too
```

`now() - max(created_at)` at the time of the disaster **is your achieved RPO**. Record it.

## 5. Reopen

```bash
kubectl -n datareliability scale deployment/datareliability-api --replicas=2
kubectl -n datareliability rollout status deployment/datareliability-api
kubectl -n datareliability scale deployment/loadgen --replicas=1
```

RTO ends when real requests succeed consistently, not when the pods are Ready.

## AWS (RDS) equivalent

```bash
aws rds describe-db-instances --db-instance-identifier <id> \
  --query 'DBInstances[0].LatestRestorableTime'
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier <id> \
  --target-db-instance-identifier <id>-restored \
  --restore-time <ISO-8601 just before the incident>   # or --use-latest-restorable-time
```

PITR restores to a **new** instance. Repoint the application (`database.host`) and keep the old one
for investigation. `LatestRestorableTime` is typically within the last 5 minutes. That, not the
nightly snapshot, is what makes a 5-minute RPO realistic on RDS.
