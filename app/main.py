"""DataReliability customer-records API.

A deliberately small stand-in for an enterprise data-management SaaS: it
creates, reads, updates and looks up customer records in PostgreSQL. The
interesting parts are operational, not functional:

- /healthz and /readyz are separate on purpose (liveness vs readiness).
- Every request is measured as Prometheus metrics labelled by route
  *template*, never raw path, so cardinality stays bounded.
- Schema migrations are idempotent and run at startup, so a restored or
  rebuilt database converges back to the expected schema and indexes.
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, EmailStr, Field

APP_VERSION = os.environ.get("APP_VERSION", "dev")
POD_NAME = os.environ.get("POD_NAME", os.environ.get("HOSTNAME", "local"))

DB_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']} "
    f"connect_timeout=3 "
    f"application_name=datareliability-api"
)

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","pod":"' + POD_NAME + '","msg":"%(message)s"}',
)
log = logging.getLogger("api")

# ---------------------------------------------------------------- metrics
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests served, by route template and status code.",
    ["method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency, by route template.",
    ["method", "route"],
    # 0.3s is an explicit bucket because the latency SLO threshold is 300 ms.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5, 10),
)
DB_ERRORS = Counter(
    "db_errors_total",
    "Database operations that failed because the database was unreachable or unavailable.",
    ["operation"],
)
APP_ERRORS = Counter(
    "app_errors_total",
    "Operations that failed because of an application bug (e.g. SQL not matching the schema).",
    ["operation", "error"],
)
# Pre-create error series at 0 so the first error is visible to rate() and
# increase(); a series born at 1 is invisible to both (scenario 05 finding).
for _op in ("readyz", "create", "get", "update", "search", "stats"):
    DB_ERRORS.labels(_op)

APP_INFO = Gauge("app_info", "Build information.", ["version"])
APP_INFO.labels(version=APP_VERSION).set(1)

# ---------------------------------------------------------------- database
SCHEMA = """
-- Serialise migrations: several replicas start at once and concurrent
-- CREATE ... IF NOT EXISTS can still collide in the catalog.
SELECT pg_advisory_xact_lock(4242);
CREATE TABLE IF NOT EXISTS customers (
    id          BIGSERIAL PRIMARY KEY,
    external_id UUID        NOT NULL UNIQUE,
    email       TEXT        NOT NULL,
    full_name   TEXT        NOT NULL,
    country     CHAR(2)     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customers_email_idx ON customers (email);
CREATE INDEX IF NOT EXISTS customers_created_at_idx ON customers (created_at);
"""

# The pool opens lazily and retries in the background, so the process can
# start (and pass liveness) while the database is unavailable. Readiness is
# what reports the database problem; restarting the pod would not fix it.
pool = ConnectionPool(
    DB_DSN,
    min_size=1,
    max_size=10,
    open=False,
    timeout=3,
    kwargs={"row_factory": dict_row},
)


def migrate() -> bool:
    try:
        with pool.connection() as conn:
            conn.execute(SCHEMA)
        log.info("schema migration applied")
        return True
    except Exception as exc:  # database may not be up yet
        log.warning(f"schema migration deferred: {exc}")
        return False


_migrated = False


def ensure_schema() -> None:
    global _migrated
    if not _migrated:
        _migrated = migrate()


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open(wait=False)
    ensure_schema()
    log.info(f"started version={APP_VERSION}")
    yield
    pool.close()


app = FastAPI(title="DataReliability API", version=APP_VERSION, lifespan=lifespan)


@app.middleware("http")
async def measure(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = route.path if route else "unmatched"
        if template != "/metrics":
            elapsed = time.perf_counter() - start
            REQUESTS.labels(request.method, template, str(status)).inc()
            LATENCY.labels(request.method, template).observe(elapsed)


# ---------------------------------------------------------------- models
class CustomerIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=2)


class CustomerPatch(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    country: str | None = Field(default=None, min_length=2, max_length=2)


def db_failure(operation: str, exc: Exception) -> HTTPException:
    # Classify before answering (post-incident action from scenario 04):
    # only connectivity/availability problems are "database errors" and 503
    # (retryable). A SQL/schema mismatch is a bug in *this* code: 500, counted
    # separately, so responders are not sent to look at a healthy database.
    if isinstance(exc, psycopg.OperationalError):  # includes PoolTimeout
        DB_ERRORS.labels(operation).inc()
        log.error(f"db unavailable op={operation} err={type(exc).__name__}: {exc}")
        return HTTPException(status_code=503, detail=f"database unavailable during {operation}")
    APP_ERRORS.labels(operation, type(exc).__name__).inc()
    log.error(f"app error op={operation} err={type(exc).__name__}: {exc}")
    return HTTPException(status_code=500, detail=f"internal error during {operation}")


# ---------------------------------------------------------------- routes
NOT_FOUND = {404: {"description": "Customer not found"}}
UNAVAILABLE = {503: {"description": "Database unavailable (retryable)"},
               500: {"description": "Internal error"}}

@app.get("/healthz")
def healthz():
    # Liveness: the process is alive. Deliberately does NOT touch the
    # database, so a database outage never turns into a restart storm.
    return {"status": "ok", "version": APP_VERSION}


@app.get("/readyz", responses=UNAVAILABLE)
def readyz():
    # Readiness: this pod can serve real traffic right now.
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        ensure_schema()
        return {"status": "ready", "version": APP_VERSION}
    except Exception as exc:
        raise db_failure("readyz", exc)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/customers", status_code=201, responses=UNAVAILABLE)
def create_customer(body: CustomerIn):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO customers (external_id, email, full_name, country) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), body.email, body.full_name, body.country.upper()),
            ).fetchone()
        return row
    except Exception as exc:
        raise db_failure("create", exc)


@app.get("/customers/{customer_id}", responses={**NOT_FOUND, **UNAVAILABLE})
def get_customer(customer_id: int):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE id = %s", (customer_id,)
            ).fetchone()
    except Exception as exc:
        raise db_failure("get", exc)
    if row is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return row


@app.patch("/customers/{customer_id}",
           responses={**NOT_FOUND, **UNAVAILABLE, 422: {"description": "No fields to update"}})
def update_customer(customer_id: int, body: CustomerPatch):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "country" in fields:
        fields["country"] = fields["country"].upper()
    assignments = ", ".join(f"{name} = %({name})s" for name in fields)
    try:
        with pool.connection() as conn:
            row = conn.execute(
                f"UPDATE customers SET {assignments}, updated_at = now() "
                "WHERE id = %(id)s RETURNING *",
                {**fields, "id": customer_id},
            ).fetchone()
    except Exception as exc:
        raise db_failure("update", exc)
    if row is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return row


@app.get("/customers", responses=UNAVAILABLE)
def find_customers(email: str, limit: int = 10):
    try:
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customers WHERE email = %s ORDER BY id LIMIT %s",
                (email, min(limit, 100)),
            ).fetchall()
        return rows
    except Exception as exc:
        raise db_failure("search", exc)


@app.get("/stats", responses=UNAVAILABLE)
def stats():
    # Used by the DR drill to measure recovery point: the newest committed
    # record tells us how much recent data survived a restore.
    try:
        with pool.connection() as conn:
            return conn.execute(
                "SELECT count(*) AS customers, max(id) AS max_id, "
                "max(created_at) AS newest_record_at FROM customers"
            ).fetchone()
    except Exception as exc:
        raise db_failure("stats", exc)
