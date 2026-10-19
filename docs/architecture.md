# Architecture

> This is a simplified lab architecture for a fictional data-management SaaS. It is **not** a
> diagram of Semarchy's internal SaaS deployment. It borrows only the publicly documented shape
> of Semarchy's self-hosted platform: Kubernetes services in front of PostgreSQL (RDS on AWS),
> with object storage, monitoring and staged dependencies.

## Local lab (Milestone 1, $0)

```mermaid
flowchart LR
  subgraph host["Developer machine (Docker)"]
    probe["lab.py probe<br/>outside-in write+read every 1s"]
    subgraph kind["kind cluster · Kubernetes 1.35"]
      subgraph cp["control-plane node"]
        pg[("PostgreSQL 16<br/>StatefulSet + PVC")]
        lg["loadgen<br/>~10 rps, client-side SLI"]
        prom["Prometheus + Alertmanager"]
        graf["Grafana"]
        ksm["kube-state-metrics"]
      end
      subgraph w1["worker 1"]
        api1["api pod"]
      end
      subgraph w2["worker 2"]
        api2["api pod"]
      end
      bk["postgres-backup<br/>CronJob */5"]
    end
    s3[("SeaweedFS S3<br/>off-cluster backups")]
  end

  probe -->|NodePort 30080| api1 & api2
  lg --> api1 & api2
  api1 & api2 --> pg
  bk -->|pg_dump| pg
  bk -->|aws s3 cp| s3
  prom -.scrape.-> api1 & api2 & lg & ksm
  graf --> prom
```

### Placement decisions

| Component | Where | Why |
|---|---|---|
| API replicas | Workers, spread by `topologySpreadConstraints` | One node failure leaves one replica serving |
| PostgreSQL, loadgen, Prometheus, Grafana | Control-plane node | Worker-failure drills test the app tier without also blinding the monitoring or killing the single DB instance |
| Backups | SeaweedFS **outside** the cluster | A backup inside the blast radius is not a backup. Survives PVC loss and `kind delete cluster` |

### Health model

| Endpoint | Probe | Touches DB? | Why |
|---|---|---|---|
| `/healthz` | liveness, startup | No | A DB outage must not trigger a restart storm; restarting cannot fix the DB |
| `/readyz` | readiness | Yes | Pull a pod out of the Service when it cannot serve real requests |

Readiness does **not** prove correctness. A release with broken SQL passes it (scenario 04).
That is why CI runs integration tests against a real PostgreSQL.

## AWS (Milestone 2, ephemeral)

```mermaid
flowchart LR
  subgraph vpc["VPC 10.40.0.0/16 · 2 AZs"]
    subgraph pub["public subnets"]
      nat["NAT gateway (single, see ADR 003)"]
    end
    subgraph priv["private subnets"]
      ng["EKS managed node group<br/>2× t3.medium Spot, IMDSv2 hop=1"]
    end
    subgraph db["database subnets"]
      rds[("RDS PostgreSQL 16<br/>encrypted, PITR, optional Multi-AZ")]
    end
  end
  eks["EKS 1.34 control plane<br/>api/audit logs → CloudWatch"] --- ng
  ng -->|SG: 5432 from nodes only| rds
  ng -->|Pod Identity: put/get only| s3b[("S3 backups<br/>versioned, TLS-only, 7-day lifecycle")]
  ecr["ECR<br/>immutable tags, scan on push"] --> ng
  sm["Secrets Manager<br/>RDS-managed master password"] -.-> rds
  budget["AWS Budget $20<br/>50/80/100% + forecast"]
  gd["GuardDuty + EKS audit<br/>(opt-in)"]
```

| Local | AWS | What changes |
|---|---|---|
| kind | EKS | Managed control plane, real AZs |
| PostgreSQL StatefulSet | RDS PostgreSQL 16 | PITR replaces 5-minute dumps as the primary RPO mechanism |
| SeaweedFS | S3 | Same `aws s3` commands in `backup.sh`; only the endpoint changes |
| Kubernetes Secret created by `lab.py` | Secrets Manager (RDS-managed) | Rotation is owned by AWS |
| `kind load docker-image` | ECR | Immutable tags make `helm rollback` trustworthy |
