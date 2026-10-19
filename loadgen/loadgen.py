"""Synthetic client traffic plus a client-side (black-box) SLI.

The API measures itself (white-box). That stops working when every API pod
is down or unreachable: a dead process exports no error metrics. This
generator measures the same requests from the caller's side, so outages
show up in Prometheus even when the server cannot report them.

Traffic mix (per request): 60% read by id, 15% lookup by email,
20% create, 5% update. Creates continuously add new records, which is what
lets the DR drill measure recovery point (newest surviving record).
"""

import os
import random
import string
import threading
import time

import httpx
from prometheus_client import Counter, Histogram, start_http_server

TARGET = os.environ.get("TARGET_URL", "http://datareliability-api")
RPS = float(os.environ.get("RPS", "10"))
WORKERS = int(os.environ.get("WORKERS", "8"))
TIMEOUT = float(os.environ.get("TIMEOUT_SECONDS", "5"))

REQUESTS = Counter(
    "loadgen_requests_total",
    "Requests issued by the synthetic client, by operation and outcome.",
    ["operation", "outcome"],  # outcome: success | http_4xx | http_5xx | error
)
LATENCY = Histogram(
    "loadgen_request_duration_seconds",
    "Client-observed latency, including network and timeouts.",
    ["operation"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5, 10),
)

# Export every label combination at 0 from the start. Prometheus' increase()
# and rate() cannot count a series' first sample, so a counter that only
# appears on its first error under-reports that error (found in scenario 05).
for _op in ("create", "update", "search", "read"):
    for _outcome in ("success", "http_4xx", "http_5xx", "error"):
        REQUESTS.labels(_op, _outcome)

known_ids: list[int] = []
known_emails: list[str] = []
lock = threading.Lock()


def remember(row: dict) -> None:
    with lock:
        known_ids.append(row["id"])
        known_emails.append(row["email"])
        if len(known_ids) > 5000:
            del known_ids[:1000]
            del known_emails[:1000]


def pick(seq: list):
    with lock:
        return random.choice(seq) if seq else None


def random_customer() -> dict:
    tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return {
        "email": f"lg-{tag}@example.com",
        "full_name": f"Synthetic {tag}",
        "country": random.choice(["US", "GB", "FR", "DE", "CA", "GH"]),
    }


def one_request(client: httpx.Client) -> None:
    roll = random.random()
    if roll < 0.20 or not known_ids:
        op, method, url, body = "create", "POST", "/customers", random_customer()
    elif roll < 0.25:
        op, method, url, body = "update", "PATCH", f"/customers/{pick(known_ids)}", {
            "full_name": f"Updated {random.randint(0, 9999)}"
        }
    elif roll < 0.40:
        op, method, url, body = "search", "GET", "/customers", None
    else:
        op, method, url, body = "read", "GET", f"/customers/{pick(known_ids)}", None

    params = {"email": pick(known_emails)} if op == "search" else None
    start = time.perf_counter()
    try:
        resp = client.request(method, url, json=body, params=params)
        if resp.status_code >= 500:
            outcome = "http_5xx"
        elif resp.status_code >= 400:
            outcome = "http_4xx"
        else:
            outcome = "success"
            if op == "create":
                remember(resp.json())
    except httpx.HTTPError:
        outcome = "error"
    LATENCY.labels(op).observe(time.perf_counter() - start)
    REQUESTS.labels(op, outcome).inc()


def worker() -> None:
    interval = WORKERS / RPS
    with httpx.Client(base_url=TARGET, timeout=TIMEOUT) as client:
        while True:
            started = time.monotonic()
            one_request(client)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    start_http_server(9100)
    print(f"loadgen -> {TARGET} at ~{RPS} rps with {WORKERS} workers", flush=True)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    while True:
        time.sleep(3600)
