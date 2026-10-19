# Runbook: Pod restarting / CrashLoopBackOff

**Alerts:** `APIPodRestarting` (page), `KubePodCrashLooping` (from kube-prometheus-stack, slower), `APIReplicasUnavailable`

## 1. Why did the container die?

```bash
kubectl -n datareliability get pods -l app.kubernetes.io/name=datareliability-api -o wide
kubectl -n datareliability describe pod <pod>      # Last State, Reason, Exit Code, Events
kubectl -n datareliability logs <pod> --previous   # output of the crashed container
```

| Last State reason / exit code | Meaning | Usual fix |
|---|---|---|
| `OOMKilled` / 137 | Exceeded memory **limit** | Raise the limit, or find the leak |
| `Error` / 1 | Application exited (config, import error) | `logs --previous` |
| `Error` / 137 without OOM | Killed by liveness probe | Probe too strict, or app hung |
| `Completed` / 0 | Process exited normally (wrong command) | Container command |

## 2. Is it customer-impacting?

The Deployment uses `maxUnavailable: 0`. During a bad rollout the **old ReplicaSet keeps serving**
and the rollout stalls instead of taking the service down:

```bash
kubectl -n datareliability rollout status deployment/datareliability-api --timeout=5s
kubectl -n datareliability get rs -l app.kubernetes.io/name=datareliability-api
```

If old pods are still Ready, this is a failed change, not an outage. Fix it calmly.

## 3. Mitigate

```bash
helm -n datareliability history api
helm -n datareliability rollback api --wait     # back to last known-good values
```

## 4. Prevent

- Memory limits are sized from observed usage (Grafana → *API CPU and memory vs limits*) plus headroom,
  not guessed.
- Every values change goes through the same pipeline as code.
