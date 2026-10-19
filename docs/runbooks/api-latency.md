# Runbook: API latency

**Alerts:** `APILatencyFastBurn` (page), `APILatencySlowBurn` (ticket)
**SLO:** 95% of API requests complete in under 300 ms over 30 days

## 1. Where is the time going?

Grafana → *Latency (p50 / p95 / p99)* and *PostgreSQL CPU*.

- **App CPU at limit, DB CPU low** → the API is CPU-throttled: check `resources.limits` and replica count.
- **DB CPU high** → a query is doing too much work. Continue below.
- **Only one route slow** → that route's query plan.

## 2. Find the expensive query

```bash
kubectl -n datareliability exec postgres-0 -- psql -U app -d customers -c \
  "SELECT round(extract(epoch FROM now()-query_start)::numeric,3) AS secs, state, left(query,80)
     FROM pg_stat_activity WHERE datname='customers' AND state <> 'idle' ORDER BY secs DESC;"

# Sequential scans on a large table are the usual suspect
kubectl -n datareliability exec postgres-0 -- psql -U app -d customers -c \
  "SELECT relname, seq_scan, seq_tup_read, idx_scan FROM pg_stat_user_tables;"

# PostgreSQL logs every statement slower than 250 ms
kubectl -n datareliability logs postgres-0 --since=10m | grep duration
```

Confirm with `EXPLAIN ANALYZE` on the slow statement: `Seq Scan` with millions of rows removed by filter = missing index.

## 3. Mitigate

```sql
-- CONCURRENTLY: builds without taking a write lock on the table.
CREATE INDEX CONCURRENTLY IF NOT EXISTS customers_email_idx ON customers (email);
```

Then check: is the index `VALID`? A failed concurrent build leaves an invalid index behind that you must drop and recreate.

```sql
SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indrelid = 'customers'::regclass;
```

## 4. Prevent

Schema changes, including index drops, go through migrations that are reviewed and tested against
production-sized data, not ad-hoc `psql`.
