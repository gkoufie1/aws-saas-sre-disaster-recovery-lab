# Post-incident review: <short title>

> Blameless: describe what the system and the process allowed, not who made a mistake.

| | |
|---|---|
| **Date** | YYYY-MM-DD |
| **Severity** | SEV1 (customer-facing outage) / SEV2 (degraded) / SEV3 (no customer impact) |
| **Duration of impact** | first failed request → last failed request |
| **Time to detect (TTD)** | change/failure → first alert firing |
| **Time to mitigate (TTM)** | first alert → impact ended |
| **Error budget consumed** | % of the 30-day availability / latency budget |
| **Evidence** | `evidence/<file>.json` |

## Summary

Two or three sentences a customer-facing manager could read.

## Impact

Who and what was affected, measured by the outside-in probe and SLIs, not estimated.

## Timeline (UTC)

| Time | Offset | Event |
|---|---|---|

## Detection

Which alert fired first? Was it the right one? Was anything missed or late?

## Root cause and contributing factors

The technical cause, and the conditions that let it reach production / last as long as it did.

## What went well

## What went poorly

## Action items

| Action | Type (prevent / detect / mitigate) | Owner | Status |
|---|---|---|---|

## Change-management note

Which change triggered it, how it was approved, and the evidence trail (Helm revision, Git commit,
CI run). This is the record a SOC 2 / ISO 27001 change-management control would ask for.
