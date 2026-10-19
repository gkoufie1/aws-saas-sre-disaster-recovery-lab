# Failure scenarios: hypotheses (written before running)

Each scenario is run with `python lab.py scenario <NN> run`:
steady state → inject → wait for alerts → capture diagnostics → hold 2 min (investigating) → remediate → verify.
An outside-in probe (one write + one read per second) records the real impact independently of the
system's own metrics.

These hypotheses were written **before** the first run. The incident reports compare them with what
actually happened.

| # | Failure injected | Hypothesis | Expected first alert | Remediation |
|---|---|---|---|---|
| 01 | Helm change sets API memory limit to 48 Mi | New pods are `OOMKilled` and crash-loop. Because `maxUnavailable: 0`, old pods keep serving: **a stalled rollout, not an outage** | `APIPodRestarting` within ~3 min | `helm rollback` |
| 02 | `DROP INDEX customers_email_idx` on a 1M-row table | Email lookups become sequential scans; the DB CPU limit causes latency to spread to other routes | `APILatencyFastBurn` | `CREATE INDEX CONCURRENTLY` |
| 03 | DB password rotated in PostgreSQL only, then DB restarted | Nothing breaks until connections drop; after the DB restart **every** API pod fails readiness → full outage; server-side SLI goes blind; backups also fail | `SyntheticClientFailing` | Roll the Secret forward, restart API |
| 04 | Release 1.1.0 with an `INSERT` column typo | Rollout succeeds (readiness passes); ~20% of requests (creates) return 503 | `APIAvailabilityFastBurn` within ~2 min | `helm rollback` |
| 05 | `docker stop` one worker node | ~50% errors for ~40–50 s until the node is marked NotReady; then healthy on 1 replica; replacement only after the 300 s default eviction | `APIReplicasUnavailable` | Automatic; restart node |
| DR | Delete PostgreSQL StatefulSet **and** its volume | Full outage; restore from newest off-cluster dump; RTO ≈ 3–6 min (target 15); RPO up to ~5 min (target 5), **could miss** | `SyntheticClientFailing` | Runbook: stop writes → recreate → restore → validate → reopen |

## Outcomes (added after running)

| # | Hypothesis held? | Report |
|---|---|---|
| 01 | **No.** The rollout succeeded; pods died ~5 min later under load: a real outage | [report](incidents/2026-10-19-scenario-01-oomkilled-after-rollout.md) |
| 02 | **No.** No alert fired at all: the aggregate SLO hid it. Re-run with a per-route alert detected it | [report](incidents/2026-10-19-scenario-02-index-drop.md) |
| 03 | Mostly. Wrong about server-side blindness: the burn-rate alert paged first | [report](incidents/2026-10-19-scenario-03-credential-drift.md) |
| 04 | Yes, plus two unplanned findings (error classification, dropped requests), both fixed and re-verified | [report](incidents/2026-10-19-scenario-04-bad-release.md) |
| 05 | Yes on timing; missed connection-model effects and the lack of rebalancing | [report](incidents/2026-10-19-scenario-05-node-failure.md) |
| DR | Faster than predicted (RTO 92 s); RPO met by timing; found 0-byte backups | [results](dr/dr-drill-001-results.md) |
