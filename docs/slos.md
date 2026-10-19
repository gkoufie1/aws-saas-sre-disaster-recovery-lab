# Service level objectives

These are **lab targets** chosen for a fictional customer-records API. They are not Semarchy's
published commitments or anyone's contractual SLA.

## SLIs and SLOs

| SLI | Measured from | Good event | SLO (30 days) | Error budget |
|---|---|---|---|---|
| Availability | API (`http_requests_total`) | non-5xx response | 99.9% | 0.1% ≈ 43 min of full outage |
| Latency | API (`http_request_duration_seconds`) | completed in < 300 ms | 95% | 5% of requests |
| Client availability | Synthetic client (`loadgen_requests_total`) | success, no connection error | 99.9% (informational) | — |
| Recovery time (RTO) | DR drill, outside-in probe | service restored | ≤ 15 min | per drill |
| Recovery point (RPO) | DR drill, newest surviving record | data loss window | ≤ 5 min | per drill |

`/healthz`, `/readyz` and `/metrics` are excluded: kubelet probes are not customer traffic, and
including them inflates availability.

### Why two availability SLIs

A process that is down exports no error metrics. When every API pod is unreachable, the
server-side SLI has **no data** rather than a bad value. The synthetic client measures the same
requests from outside, so total outages still register. Scenario 03 and the DR drill show the
difference.

## Alerting: multi-window, multi-burn-rate

From the [Google SRE Workbook, "Alerting on SLOs"](https://sre.google/workbook/alerting-on-slos/).
Burn rate = how fast budget is being spent relative to "exactly on budget for 30 days".

| Alert | Burn rate | Long window | Short window | Budget spent before firing | Severity |
|---|---|---|---|---|---|
| `APIAvailabilityFastBurn` | 14.4× | 1h | 5m | 2% | page |
| `APIAvailabilitySlowBurn` | 6× | 6h | 30m | 5% | ticket |
| `APILatencyFastBurn` | 6× | 1h | 5m | — | page |
| `APILatencySlowBurn` | 3× | 6h | 30m | — | ticket |

- **The short window** makes the alert *reset* quickly after recovery instead of firing for an hour.
- **The latency SLO uses lower multipliers.** A 5% budget at 14.4× would mean 72% of requests are slow
  before paging, which is far too late.

## Symptom alerts (not SLO-based)

| Alert | Why it exists alongside the SLO alerts |
|---|---|
| `SyntheticClientFailing` | Black-box: works when the API exports nothing |
| `APIPodRestarting` | The stock `KubePodCrashLooping` waits 15 min; restarts are often a failed change with no customer impact yet |
| `APIDatabaseErrors` | Points straight at the dependency |
| `APIRouteLatencyDegraded` (ticket) | Per-route p95 > 300 ms for 5 min. Added after scenario 02: one route regressed 63× while the aggregate latency SLO stayed within budget |
| `PostgresBackupStale` (7 min, page) / `PostgresBackupJobFailed` | RPO is an SLO too. 7 min is the earliest a 5-minute schedule can tell "one backup missed" from a normal gap. Exposure is already ~2 min past the 5-min target when it fires, so treat it as urgent. It was 15 min (3× the target) until scenario 03 |

All rules: [charts/datareliability-observability/templates/slo-rules.yaml](../charts/datareliability-observability/templates/slo-rules.yaml)
