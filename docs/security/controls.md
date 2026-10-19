# Security and change-management controls

This lab is **not** SOC 2 or ISO 27001 compliant or certified; compliance applies to an organisation,
not a repository. This page maps what the lab *does* to the kind of control those frameworks ask for,
and where the evidence lives, because producing that evidence is part of an SRE's job.

| Control area | What the lab does | Evidence |
|---|---|---|
| Change management | Every deploy is a Helm revision; every scenario's trigger and rollback is timestamped | `helm history api`; `evidence/*.json`; incident reports' change-management notes |
| Pre-deploy testing | Integration tests against a real PostgreSQL in CI; Helm lint/render | `.github/workflows/ci.yml` |
| Vulnerability management | Trivy image scan (fail on fixable CRITICAL); ECR scan on push | CI job `images` |
| Secure configuration | Checkov IaC scan, hard-fail, exceptions documented inline | [iac-scan.md](iac-scan.md) |
| Least privilege | Non-root containers, read-only root FS, all capabilities dropped, no ServiceAccount token; backup IAM role cannot delete | `charts/datareliability-api/templates/deployment.yaml`, `terraform/backups.tf` |
| Secrets | Local: generated per checkout, git-ignored. AWS: RDS-managed in Secrets Manager | `lab.py credentials()`, `terraform/rds.tf` |
| Encryption | RDS and S3 encrypted at rest; TLS forced to RDS and S3 | `terraform/` |
| Backup and recovery | 5-min backups off-cluster; freshness alerting; DR drill with measured RTO/RPO | [docs/dr/](../dr/) |
| Monitoring and incident response | SLO burn-rate alerts with runbook links; blameless post-incident reviews | [docs/slos.md](../slos.md), [docs/incidents/](../incidents/) |
| Threat detection | GuardDuty with EKS audit-log protection (opt-in, AWS) | `terraform/platform.tf` |
| Audit logging | EKS API/audit/authenticator logs to CloudWatch | `terraform/eks.tf` |

## Known gaps (honest list)

- Changes made directly with `helm upgrade --set` (scenarios 01, 04) bypass Git review. That *is* the
  finding in those reports. GitOps (Argo CD) would close it; see `eks-gitops-dr-drill` for that pattern.
- No admission policy (Kyverno / Gatekeeper) yet to enforce resource limits or reject `:latest` tags.
- No NetworkPolicies restricting pod-to-pod traffic.
