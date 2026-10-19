# ADR 001: Run failure drills locally first, on AWS only briefly

**Status:** Accepted · 2026-10-19

## Context

An EKS + RDS + NAT environment costs roughly $0.25–0.35/hour while running. Repeating five failure
scenarios and a DR drill while learning, re-running and fixing bugs takes many hours. The account
has a $20/month budget.

## Decision

Build every scenario, alert, runbook and drill on a local 3-node kind cluster first, at $0. Use the
Terraform stack in `terraform/` for short, planned AWS sessions only, to capture evidence for the
behaviours that need real AWS: RDS point-in-time restore, Multi-AZ failover, EKS node replacement and
IAM/Pod Identity. Destroy after each session and verify $0 afterwards.

## Consequences

- Local results measure Kubernetes and PostgreSQL behaviour, not AWS behaviour. Each report says which
  one it measured.
- The local stand-ins are chosen so tooling carries over unchanged: same Helm chart, same `backup.sh`
  (S3 API), same PostgreSQL major version, Kubernetes inside Semarchy's documented 1.32–1.35 range.
