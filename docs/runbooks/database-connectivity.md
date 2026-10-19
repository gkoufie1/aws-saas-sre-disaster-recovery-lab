# Runbook: API cannot reach / authenticate to the database

**Alerts:** `APIDatabaseErrors` (page), `SyntheticClientFailing` (page), `PostgresBackupJobFailed` (often follows)

## 1. Which layer is failing?

Work from the outside in. Each step rules out one layer.

```bash
# a) Is the database process up?
kubectl -n datareliability get pod postgres-0
kubectl -n datareliability exec postgres-0 -- pg_isready

# b) Is the Service routing to it?
kubectl -n datareliability get endpointslices -l kubernetes.io/service-name=postgres

# c) What exactly does the app say?
kubectl -n datareliability logs -l app.kubernetes.io/name=datareliability-api --tail=10 --prefix

# d) What does the server say? (auth failures are logged here)
kubectl -n datareliability logs postgres-0 --tail=30
```

| Error in app/server logs | Layer | Action |
|---|---|---|
| `connection refused` / timeout | Network / DB down | Check pod, Service, NetworkPolicy, security group (AWS) |
| `password authentication failed` | Credentials | §2 |
| `too many connections` | Capacity | Pool sizes × replicas vs `max_connections` |
| `database "x" does not exist` / relation missing | Data loss / wrong target | [disaster-recovery.md](disaster-recovery.md) |

## 2. Credential mismatch

Establish **which side is right** before changing anything. Was the rotation intentional?

- Rotation intended → **roll forward**: update the Secret to the new value, then restart the API
  (environment variables are read once at start):
  ```bash
  kubectl -n datareliability create secret generic postgres-credentials \
    --from-literal=username=app --from-literal=password='<new>' --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n datareliability rollout restart deployment/datareliability-api
  ```
- Rotation accidental → revert the database password to the Secret's value.

Remember the **backup CronJob uses the same Secret**. Confirm the next backup succeeds.

## 3. Why this can hide for hours

Pooled connections that authenticated before a rotation keep working. The failure appears only
when connections are re-established: a database restart, failover, or pod restart. A rotation
is not finished until a fresh connection has been tested.

## 4. On AWS

RDS with `manage_master_user_password` keeps the password in Secrets Manager and rotates it there.
The application should read the secret at connect time (or use IAM database auth) rather than
relying on a copied value.
