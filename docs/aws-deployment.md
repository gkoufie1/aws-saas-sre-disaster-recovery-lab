# AWS deployment (Milestone 2): what was built and what it proves

Deployed 2026-10-19 to **us-east-2** from [`terraform/`](../terraform/) (80 resources, 11 min apply) and
[`lab_aws.py`](../lab_aws.py), used for [DR Drill 002](dr/dr-drill-002-rds-pitr-results.md), then destroyed.
Account identifiers are redacted in the screenshots.

## Platform

**EKS 1.35, inside Semarchy's documented 1.32–1.35 range, matching the local lab.** 22 pods, 1 Pod Identity association.
![EKS cluster overview](screenshots/aws/06-eks-cluster-1.35.png)

**Two Spot nodes (t3.medium) from one managed node group**, in two AZs. Spot keeps an ephemeral lab cheap; an
interruption would have been useful failure data.
![EKS compute: Spot nodes](screenshots/aws/07-eks-spot-nodes.png)

**Managed add-ons, including the Pod Identity Agent**, which lets the backup job use an IAM role with no keys in the cluster.
![EKS add-ons](screenshots/aws/08-eks-addons-pod-identity.png)

**A dedicated VPC** next to the account's default VPC: public, private and database subnets across 2 AZs, one NAT gateway ([ADR 003](adr/003-single-nat-gateway.md)).
![VPCs](screenshots/aws/09-vpc-list.png)

## Data and recovery

**After DR Drill 002: the original instance, kept unmodified for investigation, beside the point-in-time
restored instance now serving the application.**
![RDS instances](screenshots/aws/04-rds-original-and-pitr-instances.png)
![RDS dashboard](screenshots/aws/05-rds-dashboard-2-instances.png)

**Logical backups to S3 every 5 minutes, written through Pod Identity.** The size column is where the
silent data loss showed up (−16.7% after the DELETE). See [DR Drill 002, finding 5](dr/dr-drill-002-rds-pitr-results.md).
![S3 backup objects](screenshots/aws/10-s3-backups-size-drop.png)
![S3 backup prefix](screenshots/aws/11-s3-backup-folder.png)

## Security

**The backup role has one inline policy (`backup-s3`): list, plus get/put under `postgres/*`. No delete.**
Verified with a denied request, not just by reading the policy:
[`evidence/aws/iam-least-privilege-check.txt`](../evidence/aws/iam-least-privilege-check.txt).
![IAM backup role](screenshots/aws/12-iam-backup-role.png)

**ECR: immutable tags** (a tag always means the same image, which makes rollbacks trustworthy) and encryption at rest.
![ECR repositories](screenshots/aws/13-ecr-immutable-repos.png)

**The RDS master password lives only in Secrets Manager**, managed by RDS, never in Terraform state or Git,
**with automatic 7-day rotation.** That rotation would break this deployment's copied Kubernetes Secret.
See [DR Drill 002, finding 6](dr/dr-drill-002-rds-pitr-results.md).
![Secrets Manager secret](screenshots/aws/14-secrets-manager-rds-secret.png)
![Secret rotation](screenshots/aws/15-secrets-manager-7-day-rotation.png)

## Audit and observability

**EKS control-plane logs (API, audit, authenticator) and RDS PostgreSQL logs in CloudWatch**, with 1-week retention.
![CloudWatch log groups](screenshots/aws/16-cloudwatch-log-groups.png)
![EKS audit log group](screenshots/aws/17-cloudwatch-eks-audit-logs.png)

**The same SLO dashboard as the local lab, running on EKS during the drill.** Server-side availability stayed
at **100%** while a sixth of the customers were missing. The red synthetic-client figure (99.86%) is mostly the
planned 13-second write freeze during cutover. (*PostgreSQL CPU: no data* is expected: on AWS the database is
RDS, not a pod, so its CPU is in CloudWatch.)
![Grafana SLO overview, 1 hour](screenshots/aws/01-grafana-slo-overview-1h.png)
![Grafana SLO overview, 30 minutes](screenshots/aws/02-grafana-slo-overview-30m.png)
![Grafana pods, errors, resources](screenshots/aws/03-grafana-pods-errors-resources.png)
