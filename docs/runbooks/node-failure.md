# Runbook: Node failure

**Alerts:** `APIReplicasUnavailable` (warning), `KubeNodeNotReady` (kube-prometheus-stack), `SyntheticClientFailing` if capacity is short

## 1. Confirm

```bash
kubectl get nodes
kubectl describe node <node>          # Conditions, taints: node.kubernetes.io/unreachable
kubectl -n datareliability get pods -o wide
```

## 2. What Kubernetes does on its own, and how long it takes (measured)

| Phase | Measured in this lab | What happens |
|---|---|---|
| Node stops heartbeating | 0 s | Pods on it still look `Running`; new connections routed to them time out |
| Node tainted `unreachable` | **~50 s** | Pods marked not Ready → removed from Service endpoints → **errors stop** |
| Pods evicted, replacement Running | **+305 s** default · **+31 s** with this chart's `nodeFailureTolerationSeconds: 30` | Deployment recreates the pod on a healthy node |

Source: [scenario 05 report](../incidents/2026-10-19-scenario-05-node-failure.md). The ~50 s error
window is set by the control plane and is not changed by the toleration.

## 3. Mitigate

- Capacity is short and replacements have not started → scale up to absorb load on surviving nodes:
  `kubectl -n datareliability scale deployment/datareliability-api --replicas=3`
- Node will not come back → `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`, then
  `kubectl delete node <node>`. On EKS the managed node group replaces the instance.

## 4. After the node is back: rebalance

Kubernetes does **not** move pods back. Check placement. If every replica is on one node, the service
is one failure away from an outage:

```bash
kubectl -n datareliability get pods -l app.kubernetes.io/name=datareliability-api -o wide
kubectl -n datareliability rollout restart deployment/datareliability-api   # PDB + maxUnavailable 0 keep it safe
```

This re-spreads **only because** the chart sets `matchLabelKeys: [pod-template-hash]` on its
topology spread constraint. Without it the restart was tested and put **both** replicas on one node:
the scheduler counted the old revision's still-terminating pods.

## 5. Prevent / tune

- `topologySpreadConstraints` keep replicas on different nodes (and zones on EKS).
- `PodDisruptionBudget` protects voluntary disruptions (drains, upgrades), **not** node crashes.
- `nodeFailureTolerationSeconds` in the chart lowers the 300s eviction wait for this workload.
