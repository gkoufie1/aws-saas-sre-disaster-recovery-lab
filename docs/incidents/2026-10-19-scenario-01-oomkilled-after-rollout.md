# Post-incident review: API OOMKilled five minutes after a "successful" rollout

> Lab scenario 01, injected on purpose. Run on a local kind cluster (Kubernetes 1.35).
> All times UTC.

| | |
|---|---|
| **Date** | 2026-10-19 |
| **Severity** | SEV1. Customer-facing writes and reads failed |
| **Trigger** | Helm values change: API memory limit 256 Mi → 48 Mi |
| **Duration of impact** | 19:19:25 → 19:23:30 (**4 min 05 s**), intermittent at first, then near-total |
| **Time to detect (TTD)** | 86 s after impact began (`APIPodRestarting`); **6 min 43 s after the change** |
| **Time to mitigate** | 39 s from rollback to last failed request; 2 min 39 s from first page |
| **Error budget consumed** | ≈ 177 failed probe-seconds ≈ **6.8%** of the 30-day availability budget (43.2 min) |
| **Evidence** | [`evidence/scenario-01-20261019T191408Z.json`](../../evidence/scenario-01-20261019T191408Z.json) |

## Summary

A configuration change lowered the API container's memory limit to 48 Mi. The new pods started,
passed every probe, and the rollout completed, which removed the old pods. About five minutes later,
under normal traffic, both new pods exceeded the limit, were `OOMKilled`, and entered
CrashLoopBackOff. With no healthy replica left, the API was down until the change was rolled back.

## Hypothesis vs. what happened

The hypothesis, written before the run ([docs/scenarios.md](../scenarios.md)), was wrong in a useful way.

| | Expected | Actual |
|---|---|---|
| Rollout | Stalls; new pods crash during startup | **Completed** in seconds. Startup fit in 48 Mi |
| Old pods | Keep serving (`maxUnavailable: 0`) | **Already terminated** by the time the problem appeared |
| Customer impact | None | **4 minutes of outage** |

`maxUnavailable: 0` only protects against failures that appear **during** the rollout. A failure that
needs steady-state traffic to show up passes every gate a rolling update has.

## Timeline

| Time | Offset | Event |
|---|---|---|
| 19:14:08 | T+0 s | `helm upgrade --set resources.limits.memory=48Mi`; rollout completes, both pods Ready |
| 19:19:25 | T+317 s | **First failed probe request.** A short burst (3 failures over 6 s), then recovers |
| 19:20:17 | T+369 s | Failures resume and continue: both pods OOMKilled → restarting → back-off |
| 19:20:51 | T+403 s | **Page: `APIPodRestarting`** (> 2 restarts in 10 min) |
| 19:21:01 | T+413 s | Diagnostics: both pods `CrashLoopBackOff`, `Last State: OOMKilled`, exit code 137 |
| 19:22:06 | T+478 s | `APIReplicasUnavailable` (0 of 2 available for 2 min) |
| 19:23:10 | T+542 s | Responder runs `helm rollback api` (2-min investigation hold, by design) |
| 19:23:30 | T+562 s | Last failed probe request |
| 19:23:49 | T+581 s | Recovered: 10 consecutive successful write+read cycles |

## Diagnosis (what the responder saw)

```text
$ kubectl get pods
datareliability-api-67fdf7c774-pzksk   0/1  CrashLoopBackOff  2 (22s ago)  worker
datareliability-api-67fdf7c774-xg67b   0/1  CrashLoopBackOff  3 (35s ago)  worker2

$ kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState}'
{"terminated":{"exitCode":137,"reason":"OOMKilled", ...}}

Limits:  memory: 48Mi
```

Measured working set (Prometheus, `container_memory_working_set_bytes`):

| Pods | Peak working set |
|---|---|
| Healthy 256 Mi pods, same image, same traffic | **48–50 MiB** steady, one 111 MiB peak at first start |
| 48 Mi pods just before being killed | 47.8 MiB |

The limit was set at the application's normal steady-state working set, with no headroom. Startup
used less than that, so the probes passed.

## Root cause and contributing factors

- **Root cause:** a memory limit below the application's steady-state working set.
- The limit was chosen without looking at observed usage.
- Rolling-update safety (`maxUnavailable: 0`, readiness gates) cannot catch a failure that needs time
  and traffic to appear. There was no post-deploy soak or canary.
- **Both replicas had the same limit and the same traffic, so they failed together.** Two replicas
  protect against node and pod faults, not against a shared configuration fault.

## Detection

- `APIPodRestarting` paged 86 s into the impact. The stock `KubePodCrashLooping` rule waits 15 minutes
  and never fired during the incident, which is why the custom rule exists.
- **Tooling gap found:** `SyntheticClientFailing` was still firing from the previous scenario when this
  one began. The evidence recorder only logged an alert's *first* appearance, so its resolve and
  re-fire were not captured. Fixed in `lab.py`: alert tracking now records every firing and resolved
  transition, and the baseline waits for page alerts to clear before injecting.

## What went well

- The page arrived within 90 s of impact and named the failing pods.
- `kubectl describe` showed `OOMKilled` / exit 137 immediately; the diagnosis took one command.
- `helm rollback` restored the previous values in under 40 s.

## What went poorly

- Six minutes passed between the change and the page. A responder who deployed and walked away would
  not connect the two.
- Nothing stopped an 80% cut to a resource limit from being merged.

## Action items

| Action | Type | Status |
|---|---|---|
| Size limits from observed peak working set + ≥ 2× headroom; keep 128 Mi request / 256 Mi limit | prevent | Done (values unchanged; rationale documented here) |
| Alert on working set > 80% of the memory limit, *before* the OOM kill | detect | Open |
| Post-deploy soak: watch the SLO burn rate for 10 min after each release before declaring done | detect | Open (CI/CD, Milestone 3) |
| Policy check in CI: reject resource limit changes > 50% without review | prevent | Open |
| Fix the evidence recorder's alert-transition tracking | tooling | **Done** |

## Change-management note

The trigger was a values-only change (`helm upgrade --reuse-values --set ...`) made outside Git. Helm
recorded it as a new release revision, which is what made the rollback a single command. In a SOC 2 / ISO 27001
context, the finding is that **configuration changes need the same review path as code changes**.
