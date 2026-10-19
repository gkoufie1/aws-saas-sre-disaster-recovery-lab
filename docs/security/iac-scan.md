# IaC security scan and accepted exceptions

**Tool:** Checkov 3.2.400 against `terraform/` · **Date:** 2026-10-19
**Result:** 84 passed · 0 failed · 14 skipped with recorded reasons. CI fails on any new finding.

## Fixed

| Check | Finding | Fix |
|---|---|---|
| CKV_AWS_300 | Backup bucket had no rule to abort failed multipart uploads | Bucket-wide lifecycle rule: abort after 1 day. `pg_dump` streams via multipart, so a killed job would otherwise leave billed, invisible parts |

## Accepted exceptions (lab scope)

Each exception is an inline `#checkov:skip=<ID>:<reason>` on the resource, so it is reviewed with the
code it applies to.

| Check | Resource | Why accepted here | Production answer |
|---|---|---|---|
| CKV_AWS_157 | RDS | Multi-AZ is a variable, on only for the AZ-failover drill | `db_multi_az = true` |
| CKV_AWS_293 | RDS | Deletion protection off so `destroy` returns the account to $0 | On, with final snapshot |
| CKV_AWS_118 | RDS | Enhanced monitoring adds cost; slow-query logs cover the lab's needs | On |
| CKV_AWS_353 | RDS | Performance Insights not used on `db.t4g.micro` | CloudWatch Database Insights |
| CKV_AWS_18 | S3 backups | Access logging needs a second bucket | CloudTrail data events or a log bucket |
| CKV_AWS_144 | S3 backups | Cross-region replication is the next DR step | CRR to the DR region |
| CKV_AWS_145 | S3 backups | SSE-S3 (AES256) instead of a customer-managed KMS key | KMS CMK if key-level audit is required |
| CKV2_AWS_62 | S3 backups | No consumer for event notifications; backup freshness alerted from Prometheus | — |
| CKV_AWS_136 ×3 | ECR | AWS-managed encryption | KMS CMK if required |
| CKV2_AWS_3 | GuardDuty | Single account, no Organizations delegated admin | Org-wide delegated admin |
| CKV_TF_1 ×2 | VPC/EKS modules | Registry modules pinned by version + lock file | Same, or internal module mirror |

## Controls that passed and matter most

- RDS: encrypted storage, not public, TLS forced (`rds.force_ssl`), password managed by Secrets Manager
  (never in state), IAM auth enabled, automated backups ≥ 1 day (validation rejects 0).
- S3 backups: public access blocked, versioned, encrypted, TLS-only bucket policy, lifecycle expiry.
- EKS: API endpoint restricted to `admin_cidrs`; control-plane audit logs to CloudWatch; IMDSv2 required with
  hop limit 1 (pods cannot read node credentials).
- IAM: backup role via EKS Pod Identity, scoped to one ServiceAccount; `PutObject`/`GetObject` on
  `postgres/*` only. **No delete permission**, so a compromised backup job cannot erase backup history.
- ECR: immutable tags, scan on push.
