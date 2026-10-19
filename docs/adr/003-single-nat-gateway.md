# ADR 003: One NAT gateway for the AWS lab

**Status:** Accepted · 2026-10-19

## Context

A NAT gateway costs about $0.045/hour plus data processing. One per AZ doubles that for a stack that
lives a few hours.

## Decision

Use a single NAT gateway (`single_nat_gateway = true`).

## Consequences

- If the NAT's AZ fails, nodes in the other AZ lose **egress** (image pulls, AWS APIs without VPC
  endpoints). In-VPC traffic (API → RDS) is unaffected.
- This is a known single point of failure and is out of scope for the AZ-failover drill. Production
  would use `one_nat_gateway_per_az = true` or VPC endpoints for ECR, S3 and STS.
