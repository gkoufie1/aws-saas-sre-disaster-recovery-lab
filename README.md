# DataReliability: SaaS SRE & Disaster Recovery Lab

A small customer-records API (FastAPI + PostgreSQL on Kubernetes) used as a test subject for
reliability engineering: **SLOs with burn-rate alerting, five injected production failures,
a measured disaster-recovery drill, and blameless post-incident reviews written from recorded
evidence.** Terraform for the AWS version (EKS, RDS, S3) is included and validated.

> **Scope, stated plainly.** This is a personal lab, not production experience. The application is a
> stand-in for an enterprise data-management SaaS. The architecture is simplified, and it is **not** a
> model of any company's internal platform. The SLO targets are lab targets. Every number below was
> measured on a local kind cluster on 2026-10-19 and links to its raw evidence file.

## Results at a glance

| # | Failure injected | Detected by | TTD¹ | Impact | Budget² | What it taught |
|---|---|---|---|---|---|---|
| [01](docs/incidents/2026-10-19-scenario-01-oomkilled-after-rollout.md) | Memory limit cut to 48 Mi | `APIPodRestarting` | 86 s | 4 min outage | 2.0% | **Hypothesis wrong.** The rollout *succeeded*; pods died 5 min later under load, after the old ones were gone |
| [02](docs/incidents/2026-10-19-scenario-02-index-drop.md) | Index dropped on 1M-row table | run 1: **nothing**; run 2: `APIRouteLatencyDegraded` | — / 6 min 15 s | search p95 10 → 628 ms | 0% | The aggregate latency SLO hid a **63× regression** on one route; per-route alert added, re-run detected it |
| [03](docs/incidents/2026-10-19-scenario-03-credential-drift.md) | DB password rotated, Secret not | `APIAvailabilityFastBurn` | 60 s | 3 min full outage | 2.1% | Latent for 60 s, broke at the next DB restart; **backups silently failed too** |
| [04](docs/incidents/2026-10-19-scenario-04-bad-release.md) | Release with SQL/schema mismatch | `APIAvailabilityFastBurn` | 39 s | ~3 min, creates only | 1.5% | Readiness ≠ correctness. Found and **fixed** misleading error classification and dropped requests on rollout; re-ran to verify |
| [05](docs/incidents/2026-10-19-scenario-05-node-failure.md) | Worker node killed | none needed (self-healed) | — | ~50 s of partial errors | ≤ 0.02% | One replica for **5 min → 30 s** after tuning eviction; impact depends on the client's connection model; **no rebalancing after recovery** |
| [DR](docs/dr/dr-drill-001-results.md) | DB StatefulSet **and volume** deleted | `APIAvailabilityFastBurn` | 45 s | 81 s | 0.5% | **RTO 92 s / RPO 216 s, both met, RPO by timing not design.** Found **0-byte "backups"** left by failed jobs |

¹ Time from impact to first page/alert. ² Share of the 30-day availability error budget (99.9% at the nominal 10 rps = 25,920 failed requests), from Prometheus counters.

### Bugs this lab found in its own design (and fixed)

| Found by | Problem | Fix |
|---|---|---|
| Scenario 04 | SQL bugs reported as `503 database error` → wrong alert, misleading retry signal | Classify `OperationalError` → 503 vs everything else → 500 + `app_errors_total` |
| Scenario 04 | Rollouts dropped in-flight requests | `preStop` sleep; verified 0 timeouts on re-run |
| Scenario 02 | One route degraded 63× with no alert | Per-route p95 recording rule + `APIRouteLatencyDegraded` |
| Scenario 03 | Backup-stale alert threshold was 3× the RPO target | 15 min → 7 min, page |
| Scenario 03 | `PostgresBackupJobFailed` kept firing an hour after recovery | Count only jobs started in the last 15 min |
| Scenario 03 + DR | Failed `pg_dump \| aws s3 cp -` uploaded **0-byte `.dump` files**; restore picks "newest" | Dump → validate with `pg_restore -l` → upload; restore skips invalid archives; `verify-backup` command |
| Scenario 05 | Error counters that appear on first error are invisible to `increase()`/`rate()` | Pre-initialise label sets to 0; re-run counted correctly |
| Scenario 05 | Default 300 s eviction left one replica serving for ~5 min after a node died | `nodeFailureTolerationSeconds: 30`; re-run measured ~30 s |
| Scenario 05 follow-up | `rollout restart` stacked both replicas on one node (terminating pods skew spreading) | `matchLabelKeys: [pod-template-hash]`; verified over 3 rollouts |
| Scenario 01 | Evidence recorder missed alerts that re-fired | Transition-based alert tracking; baseline waits for pages to clear |
| First CI run on GitHub | Trivy blocked the backup image: CRITICAL CVE in `gosu` inherited from the `postgres` base image | Removed the unused binary (not suppressed); backup image 1.0.2 |

## Architecture

```mermaid
flowchart LR
  probe["lab.py outside-in probe"] -->|NodePort| api
  lg["loadgen (client-side SLI)"] --> api
  subgraph k8s["kind · Kubernetes 1.35 · 3 nodes"]
    api["API ×2<br/>spread across workers"] --> pg[("PostgreSQL 16")]
    bk["backup CronJob */5"] --> pg
    prom["Prometheus · Alertmanager · Grafana"] -.scrape.-> api & lg
  end
  bk -->|validated pg_dump| s3[("SeaweedFS S3<br/>outside the cluster")]
```

Full design, placement decisions, health model and the AWS mapping: [docs/architecture.md](docs/architecture.md).

| Layer | Local (Milestone 1, $0) | AWS (Milestone 2, Terraform) |
|---|---|---|
| Kubernetes | kind 1.35, 1 control plane + 2 workers | EKS 1.34, managed node group (Spot), IMDSv2 hop 1 |
| Database | PostgreSQL 16 StatefulSet | RDS PostgreSQL 16, encrypted, PITR, Secrets Manager password |
| Backups | `pg_dump` → SeaweedFS (S3 API) | `pg_dump` → S3 (same script) + RDS automated backups |
| Delivery | Helm chart | Helm + ECR (immutable tags, scan on push) |
| Observability | kube-prometheus-stack, SLO rules chart, Grafana dashboard | same, plus CloudWatch control-plane logs |
| Guardrails | — | AWS Budget ($20, 50/80/100% + forecast), GuardDuty (opt-in) |

## SLOs

| SLI | SLO (30 d) | Alerting |
|---|---|---|
| Availability (non-5xx) | 99.9% | Multi-window burn rate: 14.4× (1h & 5m) page, 6× (6h & 30m) ticket |
| Latency (< 300 ms) | 95% | 6× page, 3× ticket, plus per-route p95 ticket |
| Client-side availability | informational | `SyntheticClientFailing`, black-box, catches outages where the API exports nothing |
| RTO / RPO | 15 min / 5 min | DR drill; backup freshness page at 7 min |

Rationale, burn-rate maths and why there are two availability SLIs: [docs/slos.md](docs/slos.md).

## Run it

Requirements: Docker (8 GB+ RAM for the VM), `kind`, `kubectl`, `helm`, Python 3.11+, AWS CLI (used
against the local S3 store). Cross-platform; developed on Windows 11.

```bash
python lab.py up                  # cluster, object store, monitoring, Postgres, API, backups, traffic (~8 min)
python lab.py seed 1000000        # 1M synthetic customers (makes scenario 02 meaningful)
python lab.py scenario 04 run     # inject → detect → diagnose → remediate → verify, writes evidence/
python lab.py dr-drill            # destroy the database volume, restore, measure RTO/RPO
python lab.py verify-backup       # restore newest backup into a scratch DB and check it
python lab.py down                # delete everything (add --keep-backups to keep the object store volume)
```

| | URL |
|---|---|
| API docs | http://127.0.0.1:30080/docs |
| Grafana: *DataReliability — SLO Overview* | http://127.0.0.1:30300 (anonymous view) |
| Prometheus alerts | http://127.0.0.1:30090/alerts |

## Repository map

```
app/                       FastAPI service + integration tests (real PostgreSQL)
loadgen/                   synthetic traffic and client-side SLI
backup/                    validated pg_dump -> S3, restore with archive validation
charts/datareliability-api/            Helm chart: probes, spread, PDB, preStop, ServiceMonitor
charts/datareliability-observability/  SLO recording rules, burn-rate alerts, Grafana dashboard
k8s/                       kind config, PostgreSQL, backup CronJob, loadgen
terraform/                 AWS: VPC, EKS, RDS, S3, ECR, Pod Identity, Budget, GuardDuty
lab.py                     lab driver: up/down, scenarios, DR drill, evidence recorder
scripts/                   PromQL helpers for incident numbers
evidence/                  raw JSON timelines from every run
docs/                      architecture, SLOs, scenarios, runbooks, incidents, DR, ADRs, security
.github/workflows/ci.yml   tests, Helm lint, Terraform validate + Checkov, Trivy (actions pinned by SHA)
```

## Documentation

- **Runbooks** (linked from every alert): [availability](docs/runbooks/api-availability.md) ·
  [latency](docs/runbooks/api-latency.md) · [crash loop](docs/runbooks/pod-crashloop.md) ·
  [database connectivity](docs/runbooks/database-connectivity.md) · [node failure](docs/runbooks/node-failure.md) ·
  [disaster recovery](docs/runbooks/disaster-recovery.md)
- **Post-incident reviews:** [docs/incidents/](docs/incidents/) ([template](docs/incidents/TEMPLATE.md))
- **Hypotheses written before running:** [docs/scenarios.md](docs/scenarios.md)
- **Decisions:** [ADR 001 local-first](docs/adr/001-local-first.md) ·
  [ADR 002 dumps vs PITR](docs/adr/002-logical-backups-and-pitr.md) ·
  [ADR 003 single NAT](docs/adr/003-single-nat-gateway.md)
- **Security:** [controls and evidence](docs/security/controls.md) · [IaC scan + accepted exceptions](docs/security/iac-scan.md)

## Cost

Milestone 1 runs entirely on a laptop: **$0**. The AWS stack is built to live for hours, not months:
one NAT gateway, Spot nodes, `db.t4g.micro`, no deletion protection, `force_destroy` on buckets and
registries. An AWS Budget with email alerts is part of the stack. **Not yet applied.** Milestone 2
will run the RDS point-in-time-restore drill and an AZ failover, then destroy and verify $0.

## Roadmap

- [x] **Milestone 1:** local lab, SLOs, 5 scenarios, DR drill, reports, fixes re-verified
- [ ] **Milestone 2:** AWS apply → DR Drill 002 (RDS PITR, measured `LatestRestorableTime`) → Multi-AZ failover → destroy, $0 verified
- [ ] **Milestone 3:** GitOps delivery with post-deploy SLO gate and auto-rollback; WAL archiving;
      memory-headroom alert; auth-failure alert; human-run (unscripted) DR drill
