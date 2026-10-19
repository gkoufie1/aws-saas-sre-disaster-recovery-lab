"""Ad-hoc PromQL against the lab's Prometheus: python scripts/promq.py '<expr>' [...]

Used to pull numbers for incident reports, e.g. peak memory per pod:
    python scripts/promq.py 'max_over_time(container_memory_working_set_bytes{container="api"}[30m]) / 2^20'
"""

import json
import sys
import urllib.parse
import urllib.request

PROM_URL = "http://127.0.0.1:30090"

for expr in sys.argv[1:]:
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    result = json.load(urllib.request.urlopen(url))["data"]["result"]
    print(f"# {expr}")
    for row in result:
        labels = {k: v for k, v in row["metric"].items() if k in ("pod", "route", "status", "outcome", "alertname", "operation")}
        print(f"  {labels or row['metric']}  {float(row['value'][1]):.4g}")
    if not result:
        print("  (no data)")
