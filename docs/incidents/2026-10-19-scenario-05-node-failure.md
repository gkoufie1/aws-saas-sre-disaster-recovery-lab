# Post-incident review: worker node lost

> Lab scenario 05, injected on purpose (`docker stop` on a kind worker node, the local equivalent
> of an instance dying) and **run twice**: with Kubernetes' default eviction timing, then with a 30 s
> toleration. Local kind cluster, Kubernetes 1.35. All times UTC.

| | Run 1 (default, 300 s toleration) | Run 2 (`nodeFailureTolerationSeconds: 30`) |
|---|---|---|
| **Severity** | SEV3: partial errors for under a minute | SEV3 |
| **Error window** (probe) | 20:06:44 → 20:07:34 (**~50 s**), 17 failed cycles | 20:32:13 → 20:33:01 (**~48 s**), 15 failed cycles |
| **Node marked NotReady / tainted** | ~50 s after loss | 50 s after loss (taint 20:33:02) |
| **Replacement pod Running** | **356 s** after loss (≈ 305 s after NotReady) | **83 s** after loss (≈ 31 s after taint) |
| **Time on one replica** | **~5 min** | **~30 s** |
| **Alerts** | `APIReplicasUnavailable` (warning) at 206 s, *after* the errors had stopped | **none** (back to 2 replicas before the 2 min `for`) |
| **Error budget** | ~0% | 4 failed client requests = 0.02% |
| **Evidence** | `evidence/scenario-05-20261019T200643Z.json` | `evidence/scenario-05-20261019T203211Z.json` |

## Summary

A worker node hosting one of the two API replicas stopped abruptly. For about 50 seconds Kubernetes
did not know the node was gone, and kube-proxy kept routing a share of new connections to the dead
pod, where they timed out. When the node controller marked the node unreachable, the pod left the
Service endpoints and errors stopped. The surviving replica carried all traffic until a replacement
was scheduled: **5 minutes** with the default toleration, **30 seconds** after tuning.

## Hypothesis vs. what happened

| Hypothesis (written before) | Result |
|---|---|
| ~50% errors for ~40–50 s until NotReady | ✅ ~50 s; errors stop exactly when the node is marked unreachable |
| Then healthy on 1 replica | ✅ |
| Replacement only after the 300 s default | ✅ 305 s after NotReady |
| — | ❌ **Not predicted:** the in-cluster load generator saw almost nothing (below) |
| — | ❌ **Not predicted:** after recovery both replicas stayed on one node (below) |

## Finding 1: impact depends on the client's connection model

| Client | Connections | What it saw in the same 50 s |
|---|---|---|
| Outside-in probe (`urllib`) | New TCP connection per request → kube-proxy picks an endpoint each time | **17 failed cycles** (timeouts, `RemoteDisconnected`) |
| Load generator (`httpx`, keep-alive) | Long-lived pooled connections; only those already bound to the dead pod fail | **2 timeouts** out of ~1,190 requests |

The same infrastructure event looks like a noticeable blip to one client and is nearly invisible to
another. "What was the customer impact?" has to be answered per client type, which is another reason
to measure from outside as well as inside.

## Finding 2: the first error number was wrong, because of a Prometheus counter gotcha

The first impact calculation from `increase(loadgen_requests_total{outcome="error"}[...])` said
**0** failures. The `error` label set did not exist until the first error happened, and `increase()` /
`rate()` cannot count a series' first sample, so the first errors were invisible. **Fix:** the load
generator and the API now create every error label set at 0 at startup. Run 2 counted its 4 failures
correctly.

## Finding 3: no rebalancing after recovery

`topologySpreadConstraints` with `whenUnsatisfiable: ScheduleAnyway` act only at scheduling time. The
replacement correctly went to the surviving node, but when the failed node returned nothing moved
back:

```text
datareliability-api-...-nrh6s   Running   datareliability-worker2
datareliability-api-...-tjzz2   Running   datareliability-worker2   <- both replicas, one node
```

The service stays **one node failure away from a full outage** until something reschedules a pod.
`DoNotSchedule` would prevent this, but it would also leave the replacement `Pending` while the node
is down, which is worse during the incident. The usual answer is the Kubernetes **descheduler**
(`RemovePodsViolatingTopologySpreadConstraint`) or a deliberate `rollout restart` after node recovery.

**Testing the obvious fix found another bug.** The runbook's first draft said `rollout restart`
re-spreads the pods. Tried on the live lab, it put both replicas on the *other* node. During a
rolling update the old revision's pods (still terminating through the 5 s `preStop`) are counted by
the spread constraint, so each new pod looks best placed on the opposite node. Adding
`matchLabelKeys: [pod-template-hash]` makes the constraint count only pods of the same revision.
After that change, the upgrade and two further consecutive restarts each left one replica per node.

## Detection

- **Nothing paged in either run, which is right for this impact.** About 50 s of partial errors cost
  0–0.02% of the monthly budget. Paging someone for an event that has already healed would be noise.
- `KubeNodeNotReady` (kube-prometheus-stack) has `for: 15m` and never fired. A node can be gone for
  14 minutes with no alert naming it. In run 1 the only signal was `APIReplicasUnavailable`, and it
  fired after the errors had stopped.

## Root cause and contributing factors

- **Root cause:** loss of a node (injected).
- The error window (~50 s) is set by how fast the control plane notices a silent node
  (node-monitor grace period). **The toleration change does not shorten it**, as both runs show ~50 s.
  It only shortens the reduced-capacity window. Shrinking the error window needs client retries on
  idempotent requests, or faster node failure detection, which on EKS is not user-configurable.

## Action items

| Action | Type | Status | Verified |
|---|---|---|---|
| `nodeFailureTolerationSeconds: 30` (chart default) | mitigate | **Done** | ✅ one replica for ~30 s instead of ~5 min |
| Pre-initialise error counters at 0 | detect | **Done** | ✅ run 2 counted 4 failures |
| `matchLabelKeys: [pod-template-hash]` on the spread constraint, so rollouts don't stack replicas | mitigate | **Done** | ✅ 1 + 2 restarts, one replica per node each time |
| Post-recovery rebalance step in the runbook | mitigate | **Done** | ✅ |
| Descheduler to rebalance automatically | mitigate | Open | |
| Node-down alert with a short `for` (2–5 min), ticket severity | detect | Open | |
| Client retry with backoff for idempotent reads | mitigate | Open | |
| Three replicas across three AZs on EKS (Milestone 2) | prevent | Open | |
