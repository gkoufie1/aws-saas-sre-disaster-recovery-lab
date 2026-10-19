# Runbook: API availability (5xx / failed requests)

**Alerts:** `APIAvailabilityFastBurn` (page), `APIAvailabilitySlowBurn` (ticket), `SyntheticClientFailing` (page)
**SLO:** 99.9% of API requests return non-5xx over 30 days ([docs/slos.md](../slos.md))

## 1. Confirm and size the impact (first 5 minutes)

```bash
# Is it real, and how big? Server-side vs client-side view.
# If only SyntheticClientFailing fires, the API may be exporting nothing at all.
kubectl -n datareliability get pods -o wide
kubectl -n datareliability get endpointslices -l kubernetes.io/service-name=datareliability-api
```

In Grafana → **DataReliability — SLO Overview**:
- *Request rate by route and status*: is it one route (code path) or every route (dependency)?
- *Synthetic client outcomes*: `error` = connection failures (no endpoints / pods down); `http_5xx` = the app answered with an error.

## 2. Decide which branch you are on

| What you see | Most likely cause | Go to |
|---|---|---|
| 5xx on one route only, began at a deploy | Bad release | §3 |
| 503 on every route, `db_errors_total` rising, pods not Ready | Database unreachable / credentials | [database-connectivity.md](database-connectivity.md) |
| Pods restarting, `OOMKilled` | Resource limits | [pod-crashloop.md](pod-crashloop.md) |
| Client `error` outcomes, a node `NotReady` | Node loss | [node-failure.md](node-failure.md) |
| Database gone / data missing | Disaster | [disaster-recovery.md](disaster-recovery.md) |

## 3. Recent change? Roll back first, investigate second

```bash
helm -n datareliability history api
kubectl -n datareliability logs -l app.kubernetes.io/name=datareliability-api --tail=20 --prefix
# If the error started with the latest revision:
helm -n datareliability rollback api --wait
```

Readiness probes only prove the pod can reach the database. They do **not** prove the code works
(scenario 04). A rollout that reports "successful" can still be the cause.

## 4. Verify recovery

- `api:sli_errors:ratio_rate5m` back under 0.1%; client outcomes are all `success`.
- The fast-burn alert clears once the 5m window drops below the threshold (expect ~5 minutes).

## 5. Afterwards

Open a post-incident review from [`docs/incidents/TEMPLATE.md`](../incidents/TEMPLATE.md) if more than
2% of the monthly error budget was used, or if a page fired.
