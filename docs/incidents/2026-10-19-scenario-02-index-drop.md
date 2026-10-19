# Post-incident review: search 63× slower, and no alert fired

> Lab scenario 02, injected on purpose and **run twice**: once as built, and again after adding the
> detection this run proved was missing. Local kind cluster, 1,000,000-row `customers` table.
> All times UTC.

| | Run 1 (as built) | Run 2 (with per-route alert) |
|---|---|---|
| **Severity** | SEV3: one feature degraded, no errors | SEV3 |
| **Search latency, p95** | 10 ms → **628 ms** | 10 ms → **749 ms** |
| **Share of *all* requests ≥ 300 ms** | **2.58%** (aggregate budget is 5%) | 3.02% |
| **Detected?** | **No.** Nothing fired in 10 minutes | **Yes:** `APIRouteLatencyDegraded` at **6 min 15 s** |
| **Fix duration** | `CREATE INDEX CONCURRENTLY`: 7 s | 5 s |
| **Availability budget used** | 0% (no errors) | 0% |
| **Evidence** | `evidence/scenario-02-20261019T194343Z.json` | `evidence/scenario-02-20261019T202007Z.json` |

## Summary

`customers_email_idx` was dropped. Customer lookup by email (`GET /customers?email=`) fell back to a
parallel sequential scan over a million rows. It was 63 times slower at p95, about 330 ms per query
in PostgreSQL alone. Every other route was unaffected and nothing failed.

**In run 1, no alert fired.** The latency SLO was working exactly as designed, and the design was the
problem: search is 15% of traffic, so even with a large share of searches over 300 ms only 2.6% of
*all* requests were slow, inside the 5% aggregate budget. The SLO averaged a broken user journey
into a healthy-looking number.

## Diagnosis

```text
EXPLAIN ANALYZE SELECT * FROM customers WHERE email = 'seed-42@example.com' ORDER BY id LIMIT 10;
  Limit (actual time=325.502..332.820 rows=1)
    -> Gather  Workers Launched: 2
       -> Parallel Seq Scan on customers (actual time=272.366..277.925 rows=0 loops=3)
            Filter: (email = 'seed-42@example.com'::text)
            Rows Removed by Filter: 335291            <- x3 workers = the whole table, per lookup
  Execution Time: 333.009 ms

pg_stat_user_tables (customers):  seq_scan 2,939 · seq_tup_read 943,282,392 · idx_scan 18,508
pg_indexes: customers_pkey, customers_external_id_key, customers_created_at_idx   <- email index missing

PostgreSQL log (log_min_duration_statement = 250 ms):
  duration: 265.956 ms  execute _pg3_2: SELECT * FROM customers WHERE email = $1 ORDER BY id LIMIT $2
```

943 million rows read across 2,939 sequential scans in about 10 minutes. The slow-query log (set to 250 ms, deliberately
under the 300 ms SLO threshold) pointed at the exact statement.

## Why each signal missed it in run 1

| Signal | Why it stayed quiet |
|---|---|
| Availability SLO | No errors, just slow |
| `APILatencyFastBurn` / `SlowBurn` | Aggregate slow ratio about 2.6–3%: below the budget, never mind 3× or 6× it |
| Outside-in probe | It exercises create + read-by-id, never search. A probe only covers what it calls |
| Dashboard | The p95-by-route data existed, but nobody was watching |

## Root cause and contributing factors

- **Root cause:** an index needed by a production query path was dropped.
- **Detection gap:** latency was measured per service, not per user journey.
- Nothing ties the index to the query that depends on it. The app's migrations would recreate it,
  but only at the next pod start. A routine restart would have silently "fixed" it and erased the
  evidence.

## Action items

| Action | Type | Status | Verified |
|---|---|---|---|
| `api:route_request_duration_seconds:p95_rate5m` recording rule per `(method, route)` | detect | **Done** | |
| `APIRouteLatencyDegraded`: route p95 > 300 ms for 5 min, ticket, ignoring routes under 0.1 rps | detect | **Done** | ✅ Run 2: fired at 6 min 15 s |
| Schema changes only via reviewed migrations; never ad-hoc DDL in production | prevent | Policy | |
| Migration CI check: fail if an index used by a known query is dropped (`EXPLAIN` on fixtures) | prevent | Open | |
| Add a search call to the outside-in probe | detect | Open | |
| Production-scale data in pre-prod: at 1,000 rows this regression is invisible | prevent | Open | |

## Why a ticket and not a page

The per-route alert is deliberately `severity: ticket`. Paging is decided by budget burn: is the
service as a whole breaking its promise? One slow feature with no errors deserves same-day attention,
not a 3 a.m. wake-up. If search were a critical journey, the right move would be a **separate SLO for
search** with its own burn-rate pages, not a lower paging threshold on a symptom alert.
