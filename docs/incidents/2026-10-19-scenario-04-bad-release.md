# Post-incident review: release 1.1.0 broke customer creation and passed every health check

> Lab scenario 04, injected on purpose and **run twice**: once as built, and again after the action
> items from the first run were shipped. Local kind cluster, Kubernetes 1.35. All times UTC.

| | Run 1 (as built, API 1.0.0) | Run 2 (after fixes, API 1.0.1) |
|---|---|---|
| **Severity** | SEV2: all creates failed, reads unaffected | SEV2 |
| **Impact window** | 19:08:29 → 19:11:27 (2 min 58 s) | 19:34:44 → 19:38:11 (3 min 27 s)¹ |
| **TTD** (deploy → page) | **47 s** (`APIAvailabilityFastBurn`) | **64 s** (`APIAvailabilityFastBurn`) |
| **Rollback → last failed request** | 9 s, **with 2 dropped requests (timeouts)** | 21 s, **0 timeouts** |
| **Misleading alerts** | `APIDatabaseErrors` fired (database was healthy) | **None** |
| **Error budget (30-day, availability)** | 387 failed client requests = **1.49%** | 413 = **1.59%** |
| **Evidence** | `evidence/scenario-04-20261019T190754Z.json` | `evidence/scenario-04-20261019T193258Z.json` |

¹ Run 2's window is longer only because the scripted 2-minute investigation hold began later relative
to the deploy. Responder behaviour was identical by design.

## Summary

Release 1.1.0 renamed a column in the `INSERT` statement only (`full_name` → `fullname`). Every pod
passed its readiness probe, because `/readyz` checks that the database is reachable, not that the
code's SQL matches the schema. Kubernetes and Helm both reported a successful rollout while every
`POST /customers` failed. Reads, searches and updates kept working, so about 20% of traffic failed.

## Timeline: run 1

| Time | Event |
|---|---|
| 19:08:21 | `helm upgrade --set image.tag=1.1.0` |
| 19:08:29 | First failed request: `HTTP 503` on create |
| 19:08:33 | Helm `--wait` returns: **rollout successful**, all pods Ready |
| 19:09:08 | **Page: `APIAvailabilityFastBurn`** (5xx ratio over 1h and 5m > 1.44%) |
| 19:10:23 | `APIDatabaseErrors` fires: **misleading**, the database was fine |
| 19:10:53 | `SyntheticClientFailing` fires (5m window needed time to fill) |
| 19:11:18 | `helm rollback api` |
| 19:11:23, 19:11:27 | Two probe requests **time out** while old pods are replaced |
| 19:11:41 | Recovered (10 consecutive successful write+read cycles) |

## Diagnosis

```text
$ kubectl logs -l app.kubernetes.io/name=datareliability-api --tail=5
db error op=create err=UndefinedColumn: column "fullname" of relation "customers" does not exist
LINE 1: INSERT INTO customers (external_id, email, fullname, country...

Prometheus: sum by (route, method, status) (rate(http_requests_total{status=~"5.."}[2m]))
  POST /customers 503   1.29 req/s      <- one route, one method: a code path, not a dependency
```

The SLO dashboard's *request rate by route and status* panel answered "dependency or code?" at a
glance: only one route was failing.

## Root cause and contributing factors

- **Root cause:** a SQL statement that does not match the schema, shipped without a test that ran it
  against a real database.
- **Readiness ≠ correctness.** The rollout safety net (readiness gates, `maxUnavailable: 0`) cannot
  catch a bug in a code path that the probe does not exercise.

## Findings from run 1 beyond the injected bug

1. **The app called its own bug a database error.** Every exception in a DB call became `503 database
   error` and incremented `db_errors_total`, so `APIDatabaseErrors` paged and pointed responders at a
   healthy database. A 503 also tells clients to retry something that will never succeed.
2. **Rollouts dropped in-flight requests.** Two requests timed out during the rollback: pods received
   SIGTERM while kube-proxy on other nodes was still routing new connections to them.

## Action items

| Action | Type | Status | Verified in run 2 |
|---|---|---|---|
| Classify errors: `OperationalError` (incl. pool timeout) → 503 + `db_errors_total`; anything else → 500 + `app_errors_total{error=...}` | detect | **Done** (`app/main.py`, API 1.0.1) | ✅ Only `HTTP 500`; `APIDatabaseErrors` did not fire |
| `preStop` sleep (5 s) so pods keep serving while endpoints are withdrawn | mitigate | **Done** (chart 1.1.0) | ✅ 0 timeouts during rollback (one run; not yet proven statistically) |
| Regression test for error classification | prevent | **Done** (`test_error_classification`) | CI |
| CI integration tests run every endpoint against real PostgreSQL. **This bug fails `test_create_get_update_search`.** | prevent | **Done** (`.github/workflows/ci.yml`) | CI |
| Post-deploy verification: synthetic write+read after `helm upgrade`, auto-rollback on failure | detect | Open | |
| Deploy through Git + CI only, not `helm upgrade` from a laptop | prevent | Open (GitOps, Milestone 3) | |

## Detection notes

- The burn-rate alert beat the black-box alert by about 2 minutes in both runs. `SyntheticClientFailing`
  uses a 5-minute ratio with `for: 1m`: at 20% failures the 5m ratio only crosses 5% after about
  75 s, then must hold for 60 s. In run 2 it fired 20 s **after** the rollback. That is acceptable for
  its job, catching total outages where the API exports nothing, but it is the wrong signal for
  partial failures.
- `APIAvailabilitySlowBurn` also fired in run 1. Its 6-hour window held less than an hour of data in a
  freshly built lab, so it behaved like a fast-burn alert. A real 30-day history would dampen it.

## Change-management note

The change was a Helm upgrade with an image tag. Both the bad release and the rollback are recorded
in `helm history api` with timestamps. The control gap is that nothing required the image to pass the
integration test suite before it could be deployed.
