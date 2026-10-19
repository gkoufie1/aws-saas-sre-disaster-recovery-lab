"""Compute impact and error-budget numbers for an evidence file from Prometheus.

    python scripts/scenario_metrics.py evidence/scenario-04-*.json

Budget arithmetic (availability SLO 99.9%, 30 days):
  budget (requests) = 0.001 x observed request rate x 30 days
so "% of budget" is independent of how busy the lab happened to be.
"""

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime

PROM = "http://127.0.0.1:30090"
SLO_ERROR_BUDGET = 0.001
MONTH_S = 30 * 24 * 3600
NOMINAL_RPS = 10.0  # k8s/loadgen.yaml
API = 'job="datareliability-api",route!~"/healthz|/readyz|/metrics"'


def q(expr: str, at: float) -> list:
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": at})
    return json.load(urllib.request.urlopen(url))["data"]["result"]


def scalar(expr: str, at: float) -> float:
    r = q(expr, at)
    return float(r[0]["value"][1]) if r else 0.0


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


for path in sys.argv[1:]:
    d = json.load(open(path))
    start = ts(d["started"])
    end = ts(d["events"][-1]["t"])
    win = f"{int(end - start)}s"
    client_fail = scalar(f'sum(increase(loadgen_requests_total{{outcome=~"http_5xx|error"}}[{win}]))', end)
    client_total = scalar(f"sum(increase(loadgen_requests_total[{win}]))", end)
    server_5xx = scalar(f'sum(increase(http_requests_total{{{API},status=~"5.."}}[{win}]))', end)
    server_total = scalar(f"sum(increase(http_requests_total{{{API}}}[{win}]))", end)
    # Denominator = the configured, nominal traffic (loadgen RPS=10). The rate
    # observed *during* an outage collapses (clients block on timeouts) and
    # the rate just before can include warm-up or a previous scenario; both
    # distort the percentage. A fixed denominator makes runs comparable.
    rate = NOMINAL_RPS
    budget_requests = SLO_ERROR_BUDGET * rate * MONTH_S
    print(f"== {d['scenario']}  {d['title']}  window={win}")
    print(f"  client: {client_fail:.0f} failed / {client_total:.0f}  "
          f"-> {100 * client_fail / budget_requests:.2f}% of 30-day availability budget "
          f"(budget = {budget_requests:.0f} failed requests at {rate:.1f} rps)")
    print(f"  server: {server_5xx:.0f} 5xx / {server_total:.0f}")
    slow = scalar(f'sum(increase(http_request_duration_seconds_bucket{{{API},le="0.3"}}[{win}]))', end)
    count = scalar(f"sum(increase(http_request_duration_seconds_count{{{API}}}[{win}]))", end)
    print(f"  latency: {100 * (1 - slow / count):.2f}% of server requests >= 300 ms" if count else "  latency: n/a")
    for r in q(
        f"histogram_quantile(0.95, sum by (le, route, method) "
        f"(increase(http_request_duration_seconds_bucket{{{API}}}[{win}])))", end
    ):
        m = r["metric"]
        print(f"    p95 {m['method']:5} {m['route']:28} {float(r['value'][1]) * 1000:7.0f} ms")
