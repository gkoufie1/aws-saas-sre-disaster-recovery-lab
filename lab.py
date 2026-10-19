#!/usr/bin/env python3
"""DataReliability lab driver. Standard library only; runs on Windows/macOS/Linux.

    python lab.py up                 # build everything on a local kind cluster
    python lab.py deploy             # rebuild the API image and helm upgrade
    python lab.py status             # what is running, what is firing
    python lab.py seed [ROWS]        # bulk-load synthetic customers
    python lab.py backup-now         # trigger a backup outside the schedule
    python lab.py verify-backup      # restore the newest backup into a scratch DB and check it
    python lab.py scenario 04 run    # inject -> detect -> diagnose -> fix -> verify
    python lab.py scenario 04 inject # or drive it by hand: inject / recover
    python lab.py dr-drill           # destroy the database, restore, measure RTO/RPO
    python lab.py down               # delete the cluster and object store

Every `run` writes a timestamped evidence file to evidence/ with the probe
timeline, alert timeline and diagnostic output. Incident reports in
docs/incidents/ are written from those files, not from memory.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".lab"  # generated credentials; git-ignored
EVIDENCE = ROOT / "evidence"

CLUSTER = "datareliability"
CTX = f"kind-{CLUSTER}"
NS = "datareliability"
MON_NS = "monitoring"
API_URL = "http://127.0.0.1:30080"
PROM_URL = "http://127.0.0.1:30090"
OBJECTSTORE = "dr-objectstore"
OBJECTSTORE_IMAGE = "chrislusf/seaweedfs:3.85"
BUCKET = "datareliability-backups"
KPS_CHART_VERSION = "92.9.0"
API_SELECTOR = "app.kubernetes.io/name=datareliability-api"
API_DEPLOY = "deployment/datareliability-api"
LOADGEN_DEPLOY = "deployment/loadgen"
ROLLOUT_TIMEOUT = "--timeout=5m"

IMAGES = {
    "api": ("datareliability-api", "1.0.2", ROOT / "app"),
    "loadgen": ("datareliability-loadgen", "1.0.1", ROOT / "loadgen"),
    "backup": ("datareliability-backup", "1.0.2", ROOT / "backup"),
}


# ======================================================================== utils
def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def say(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def sh(*args: str, check: bool = True, capture: bool = False, input: str | None = None,
       timeout: int | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(args), text=True, input=input, timeout=timeout,
        capture_output=capture, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


def out(*args: str, check: bool = True) -> str:
    return sh(*args, check=check, capture=True).stdout.strip()


def kubectl(*args: str, **kw) -> subprocess.CompletedProcess:
    return sh("kubectl", "--context", CTX, *args, **kw)


def kout(*args: str, check: bool = True) -> str:
    return out("kubectl", "--context", CTX, *args, check=check)


def helm(*args: str, **kw) -> subprocess.CompletedProcess:
    return sh("helm", "--kube-context", CTX, *args, **kw)


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: float = 3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read() or b"null")


def wait_for(desc: str, predicate, timeout: int = 300, interval: float = 3) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if predicate():
                return time.monotonic() - start
        except (Exception, SystemExit):  # NOSONAR: sh() raises SystemExit; here it means "not yet"
            pass
        time.sleep(interval)
    raise SystemExit(f"timed out after {timeout}s waiting for: {desc}")


def psql(sql: str, check: bool = True) -> str:
    creds = credentials()
    return kout("exec", "-n", NS, "postgres-0", "-c", "postgres", "--",
                "env", f"PGPASSWORD={creds['db_password']}",
                "psql", "-U", creds["db_user"], "-d", "customers", "-At", "-v",
                "ON_ERROR_STOP=1", "-c", sql, check=check)


# ================================================================== credentials
def credentials() -> dict:
    """Generated once per checkout and reused, never committed."""
    path = STATE / "credentials.json"
    if path.exists():
        return json.loads(path.read_text())
    STATE.mkdir(exist_ok=True)
    creds = {
        "db_user": "app",
        "db_password": secrets.token_urlsafe(24),
        "s3_access_key": "dr" + secrets.token_hex(8),
        "s3_secret_key": secrets.token_urlsafe(32),
        "grafana_password": secrets.token_urlsafe(16),
    }
    path.write_text(json.dumps(creds, indent=2))
    return creds


def apply_secret(name: str, data: dict[str, str]) -> None:
    manifest = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": name, "namespace": NS},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }
    kubectl("apply", "-f", "-", input=json.dumps(manifest), capture=True)


# ========================================================================== up
def cluster_exists() -> bool:
    return CLUSTER in out("kind", "get", "clusters", check=False).split()


def start_objectstore(creds: dict) -> None:
    """SeaweedFS S3 gateway on the kind Docker network, *outside* the cluster."""
    running = out("docker", "ps", "-a", "--filter", f"name=^{OBJECTSTORE}$",
                  "--format", "{{.State}}", check=False)
    if running == "running":
        return
    if running:
        sh("docker", "start", OBJECTSTORE)
        return
    s3_config = {"identities": [{
        "name": "backup",
        "credentials": [{"accessKey": creds["s3_access_key"], "secretKey": creds["s3_secret_key"]}],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
    }]}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "s3.json"
        cfg.write_text(json.dumps(s3_config))
        sh("docker", "create", "--name", OBJECTSTORE, "--network", "kind",
           "-p", "127.0.0.1:8333:8333", "-v", f"{OBJECTSTORE}-data:/data",
           OBJECTSTORE_IMAGE, "server", "-dir=/data", "-s3", "-s3.config=/etc/seaweedfs/s3.json",
           capture=True)
        sh("docker", "cp", str(cfg), f"{OBJECTSTORE}:/etc/seaweedfs/s3.json", capture=True)
    sh("docker", "start", OBJECTSTORE, capture=True)


def s3(*args: str, check: bool = True) -> str:
    creds = credentials()
    env = {**os.environ, "AWS_ACCESS_KEY_ID": creds["s3_access_key"],
           "AWS_SECRET_ACCESS_KEY": creds["s3_secret_key"], "AWS_DEFAULT_REGION": "us-east-1"}
    env.pop("AWS_PROFILE", None)  # never let a real AWS profile leak into the local store
    result = subprocess.run(["aws", "--endpoint-url", "http://127.0.0.1:8333", "s3", *args],
                            env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"aws s3 {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_image(key: str, tag: str | None = None, context: Path | None = None) -> str:
    name, default_tag, default_ctx = IMAGES[key]
    ref = f"{name}:{tag or default_tag}"
    say(f"building {ref}")
    sh("docker", "build", "-q", "-t", ref, "--build-arg", f"APP_VERSION={tag or default_tag}",
       str(context or default_ctx), capture=True)
    sh("kind", "load", "docker-image", ref, "--name", CLUSTER, capture=True)
    return ref


def install_api(tag: str = IMAGES["api"][1], extra: list[str] | None = None) -> None:
    helm("upgrade", "--install", "api", str(ROOT / "charts" / "datareliability-api"),
         "-n", NS, "--set", f"image.tag={tag}", *(extra or []), "--wait", "--timeout", "5m",
         capture=True)


def cmd_up(_args: list[str]) -> None:
    creds = credentials()
    if not cluster_exists():
        say("creating kind cluster (1 control plane + 2 workers)")
        sh("kind", "create", "cluster", "--config", str(ROOT / "k8s" / "kind-config.yaml"))
    else:
        say("kind cluster already exists")

    say("starting off-cluster object store (SeaweedFS S3)")
    start_objectstore(creds)
    wait_for("S3 bucket", lambda: (s3("mb", f"s3://{BUCKET}", check=False), BUCKET in s3("ls"))[1], 120)

    for key in IMAGES:
        build_image(key)

    kubectl("create", "namespace", NS, check=False, capture=True)
    kubectl("create", "namespace", MON_NS, check=False, capture=True)
    apply_secret("postgres-credentials", {"username": creds["db_user"], "password": creds["db_password"]})
    apply_secret("objectstore-credentials", {"access_key": creds["s3_access_key"],
                                             "secret_key": creds["s3_secret_key"]})

    say(f"installing kube-prometheus-stack {KPS_CHART_VERSION} (takes a few minutes)")
    sh("helm", "repo", "add", "prometheus-community",
       "https://prometheus-community.github.io/helm-charts", check=False, capture=True)
    sh("helm", "repo", "update", "prometheus-community", capture=True)
    helm("upgrade", "--install", "kube-prometheus-stack", "prometheus-community/kube-prometheus-stack",
         "--version", KPS_CHART_VERSION, "-n", MON_NS,
         "-f", str(ROOT / "observability" / "kube-prometheus-stack-values.yaml"),
         "--set", f"grafana.adminPassword={creds['grafana_password']}",
         "--wait", "--timeout", "10m", capture=True)
    helm("upgrade", "--install", "observability", str(ROOT / "charts" / "datareliability-observability"),
         "-n", MON_NS, capture=True)

    say("deploying PostgreSQL")
    kubectl("apply", "-f", str(ROOT / "k8s" / "postgres.yaml"), capture=True)
    kubectl("rollout", "status", "statefulset/postgres", "-n", NS, "--timeout", "5m")

    say("deploying API (Helm)")
    install_api()
    kubectl("apply", "-f", str(ROOT / "k8s" / "backup.yaml"), capture=True)
    kubectl("apply", "-f", str(ROOT / "k8s" / "loadgen.yaml"), capture=True)
    kubectl("rollout", "status", LOADGEN_DEPLOY, "-n", NS, "--timeout", "3m")

    wait_for("API ready via NodePort", lambda: http_json(f"{API_URL}/readyz")[0] == 200, 120)
    say("lab is up")
    print(f"""
  API         {API_URL}/docs
  Grafana     http://127.0.0.1:30300   (admin / see .lab/credentials.json; anonymous view enabled)
  Prometheus  {PROM_URL}/alerts
  Backups     aws --endpoint-url http://127.0.0.1:8333 s3 ls s3://{BUCKET} --recursive
""")


def cmd_deploy(_args: list[str]) -> None:
    """Rebuild the API image from app/ and roll it out with the current chart."""
    build_image("api")
    say(f"helm upgrade api -> {IMAGES['api'][1]}")
    install_api()
    say("rollout complete")


def cmd_down(_args: list[str]) -> None:
    say("deleting kind cluster and object store")
    sh("kind", "delete", "cluster", "--name", CLUSTER, check=False)
    sh("docker", "rm", "-f", OBJECTSTORE, check=False, capture=True)
    if "--keep-backups" not in _args:
        sh("docker", "volume", "rm", f"{OBJECTSTORE}-data", check=False, capture=True)


def cmd_seed(args: list[str]) -> None:
    rows = int(args[0]) if args else 1_000_000
    say(f"seeding {rows:,} synthetic customers (created 30 days ago, so they never mask RPO)")
    t0 = time.monotonic()
    psql(f"""
        INSERT INTO customers (external_id, email, full_name, country, created_at, updated_at)
        SELECT gen_random_uuid(), 'seed-' || g || '@example.com', 'Seed Customer ' || g,
               (ARRAY['US','GB','FR','DE','CA','GH'])[1 + g % 6],
               now() - interval '30 days', now() - interval '30 days'
        FROM generate_series(1, {rows}) AS g;
        ANALYZE customers;
    """)
    say(f"seeded in {time.monotonic() - t0:.1f}s; table now has {psql('SELECT count(*) FROM customers')} rows")


def cmd_backup_now(_args: list[str]) -> dict:
    name = f"postgres-backup-manual-{int(time.time())}"
    kubectl("create", "job", name, "--from=cronjob/postgres-backup", "-n", NS, capture=True)
    kubectl("wait", "--for=condition=complete", f"job/{name}", "-n", NS, "--timeout=240s", capture=True)
    log = kout("logs", f"job/{name}", "-n", NS)
    result = json.loads(log.strip().splitlines()[-1])
    say(f"backup complete: {result['key']} ({result['bytes']:,} bytes, {result['seconds']}s)")
    return result


def cmd_status(_args: list[str]) -> None:
    print(kout("get", "nodes", "-o", "wide"))
    print()
    print(kout("get", "pods", "-n", NS, "-o", "wide"))
    print()
    try:
        print("firing alerts:", ", ".join(sorted(firing_alerts())) or "none")
        print("stats:", http_json(f"{API_URL}/stats")[1])
    except Exception as exc:
        print(f"(could not query: {exc})")
    print("backups:")
    print(s3("ls", f"s3://{BUCKET}/postgres/customers/", check=False) or "  none yet")


# ===================================================================== evidence
def prom_query(expr: str) -> list:
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return http_json(url)[1]["data"]["result"]


# Always-on meta alerts from kube-prometheus-stack; not signals.
IGNORED_ALERTS = {"Watchdog", "InfoInhibitor"}


def firing_alerts(severity: str | None = None) -> set[str]:
    selector = 'alertstate="firing"' + (f',severity="{severity}"' if severity else "")
    return {r["metric"]["alertname"] for r in prom_query("ALERTS{" + selector + "}")}


class Probe(threading.Thread):
    """Independent outside-in probe: one write + one read per second.

    Separate from the in-cluster load generator so the measured impact
    window does not depend on the system under test reporting on itself.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.samples: list[dict] = []
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            t, ok, detail = now(), False, ""
            started = time.perf_counter()
            try:
                status, body = http_json(f"{API_URL}/customers", "POST", {
                    "email": f"probe-{secrets.token_hex(4)}@example.com",
                    "full_name": "Outside-in probe", "country": "US"}, timeout=2)
                status2, _ = http_json(f"{API_URL}/customers/{body['id']}", timeout=2)
                ok = status == 201 and status2 == 200
                detail = f"{status}/{status2}"
            except urllib.error.HTTPError as exc:
                detail = f"HTTP {exc.code}"
            except Exception as exc:
                detail = type(exc).__name__
            self.samples.append({"t": iso(t), "ok": ok, "ms": round((time.perf_counter() - started) * 1000),
                                 "detail": detail})
            self._halt.wait(1)

    def stop(self):
        self._halt.set()
        self.join(timeout=5)

    def consecutive_ok(self, n: int) -> bool:
        tail = self.samples[-n:]
        return len(tail) == n and all(s["ok"] for s in tail)

    def summary(self, since: datetime) -> dict:
        after = [s for s in self.samples if s["t"] >= iso(since)]
        failed = [s for s in after if not s["ok"]]
        slow = [s for s in after if s["ok"] and s["ms"] > 600]  # two calls -> 2x300ms
        return {
            "samples": len(after),
            "failed": len(failed),
            "slow_over_600ms": len(slow),
            "first_failure": failed[0]["t"] if failed else None,
            "last_failure": failed[-1]["t"] if failed else None,
            "failure_details": sorted({s["detail"] for s in failed}),
            "max_ms": max((s["ms"] for s in after), default=None),
        }


@dataclass
class Incident:
    scenario: str
    title: str
    events: list[dict] = field(default_factory=list)
    alerts: dict[str, str] = field(default_factory=dict)  # name -> first seen firing
    diagnostics: dict[str, str] = field(default_factory=dict)
    t0: datetime | None = None
    _firing: set[str] = field(default_factory=set)

    def mark(self, event: str, **extra) -> datetime:
        t = now()
        self.t0 = self.t0 or t
        self.events.append({"t": iso(t), "offset_s": round((t - self.t0).total_seconds(), 1),
                            "event": event, **extra})
        say(f"T+{(t - self.t0).total_seconds():6.1f}s  {event}")
        return t

    def poll_alerts(self) -> set[str]:
        """Record every firing/resolved transition, not just first sightings,
        so an alert that resolves and fires again is still captured."""
        try:
            current = firing_alerts() - IGNORED_ALERTS
        except Exception:
            return self._firing
        for name in sorted(current - self._firing):
            if self.alerts.get(name) in (None, "before-injection"):
                self.alerts[name] = iso(now())
            self.mark(f"alert firing: {name}")
        for name in sorted(self._firing - current):
            self.mark(f"alert resolved: {name}")
        self._firing = current
        return current

    def diagnose(self, label: str, *kubectl_args: str) -> None:
        self.diagnostics[label] = kout(*kubectl_args, check=False)

    def save(self, probe: Probe) -> Path:
        EVIDENCE.mkdir(exist_ok=True)
        path = EVIDENCE / f"{self.scenario}-{self.t0:%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps({
            "scenario": self.scenario, "title": self.title, "started": iso(self.t0),
            "events": self.events, "alerts_first_firing": self.alerts,
            "probe_summary": probe.summary(self.t0), "probe_samples": probe.samples,
            "diagnostics": self.diagnostics,
        }, indent=2))
        say(f"evidence written to {path.relative_to(ROOT)}")
        return path


def await_detection(inc: Incident, expected: set[str], timeout: int = 600) -> None:
    """Wait until any expected alert fires; record every alert seen meanwhile."""
    # Only alerts that start firing after injection count: one still firing
    # from an earlier scenario would otherwise report a fake detection time.
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        inc.poll_alerts()
        if any(inc.alerts.get(a) not in (None, "before-injection") for a in expected):
            return
        time.sleep(5)
    inc.mark("detection timeout: expected alert never fired", expected=sorted(expected))


def await_recovery(inc: Incident, probe: Probe, timeout: int = 600) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        inc.poll_alerts()
        if probe.consecutive_ok(10):
            inc.mark("recovered: 10 consecutive successful probe write+read cycles")
            return
        time.sleep(2)
    inc.mark("recovery timeout")


def baseline(inc: Incident, probe: Probe, seconds: int = 30) -> None:
    # Do not stack experiments: wait for pages from a previous run to clear,
    # otherwise detection times would be measured against stale alerts.
    if firing_alerts("page"):
        say(f"waiting for page alerts to clear: {', '.join(sorted(firing_alerts('page')))}")
        wait_for("page alerts to clear", lambda: not firing_alerts("page"), timeout=1200, interval=15)
    probe.start()
    time.sleep(seconds)
    if not probe.consecutive_ok(10):
        raise SystemExit("steady state not healthy; refusing to inject a failure on top of one")
    pre = firing_alerts() - IGNORED_ALERTS
    if pre:
        say(f"note: already firing before injection: {', '.join(sorted(pre))}")
    inc.mark("steady state confirmed", already_firing=sorted(pre))
    inc.alerts.update(dict.fromkeys(pre, "before-injection"))
    inc._firing = set(pre)


# ==================================================================== scenarios
def api_pods() -> list[str]:
    return kout("get", "pods", "-n", NS, "-l", API_SELECTOR,
                "-o", "jsonpath={.items[*].metadata.name}").split()


# --- 01: OOMKilled crash loop from an undersized memory limit ----------------
def s01_inject(inc: Incident):
    inc.mark("deploy change: resources.limits.memory=48Mi (was 256Mi)")
    helm("upgrade", "api", str(ROOT / "charts" / "datareliability-api"), "-n", NS,
         "--reuse-values", "--set", "resources.limits.memory=48Mi",
         "--set", "resources.requests.memory=48Mi", capture=True)


def s01_diagnose(inc: Incident):
    inc.diagnose("pods", "get", "pods", "-n", NS, "-l", API_SELECTOR, "-o", "wide")
    inc.diagnose("rollout", "rollout", "status", API_DEPLOY, "-n", NS, "--timeout=1s")
    newest = kout("get", "pods", "-n", NS, "-l", API_SELECTOR,
                  "--sort-by=.metadata.creationTimestamp", "-o", "jsonpath={.items[-1:].metadata.name}")
    inc.diagnose("describe_newest_pod", "describe", "pod", newest, "-n", NS)
    inc.diagnose("last_state", "get", "pod", newest, "-n", NS, "-o",
                 "jsonpath={.status.containerStatuses[0].lastState}")
    inc.diagnose("events", "get", "events", "-n", NS, "--sort-by=.lastTimestamp")


def s01_recover(inc: Incident):
    inc.mark("remediation: helm rollback to previous revision")
    helm("rollback", "api", "-n", NS, "--wait", "--timeout", "5m", capture=True)


# --- 02: slow queries after a dropped index ----------------------------------
def s02_inject(inc: Incident):
    inc.mark("schema change: DROP INDEX customers_email_idx")
    psql("DROP INDEX IF EXISTS customers_email_idx")


def s02_diagnose(inc: Incident):
    inc.diagnostics["explain_search"] = psql(
        "EXPLAIN ANALYZE SELECT * FROM customers WHERE email = 'seed-42@example.com' ORDER BY id LIMIT 10")
    inc.diagnostics["active_queries"] = psql(
        "SELECT state, wait_event_type, round(extract(epoch from now()-query_start)::numeric,3) AS secs, "
        "left(query, 80) FROM pg_stat_activity WHERE datname='customers' AND state <> 'idle' ORDER BY secs DESC")
    inc.diagnostics["table_scans"] = psql(
        "SELECT seq_scan, seq_tup_read, idx_scan FROM pg_stat_user_tables WHERE relname='customers'")
    inc.diagnostics["indexes"] = psql("SELECT indexname FROM pg_indexes WHERE tablename='customers'")
    inc.diagnostics["postgres_slow_log"] = kout("logs", "postgres-0", "-n", NS, "--since=5m", "--tail=15")


def s02_recover(inc: Incident):
    inc.mark("remediation: CREATE INDEX CONCURRENTLY customers_email_idx")
    psql("CREATE INDEX CONCURRENTLY IF NOT EXISTS customers_email_idx ON customers (email)")
    inc.mark("index rebuilt")


# --- 03: database credential drift surfaced by a database restart ------------
def s03_inject(inc: Incident):
    creds = credentials()
    rotated = secrets.token_urlsafe(24)
    (STATE / "rotated_password").write_text(rotated)
    inc.mark("password rotated in PostgreSQL only (Kubernetes Secret not updated)")
    psql(f"ALTER ROLE {creds['db_user']} WITH PASSWORD '{rotated}'")
    inc.mark("existing pooled connections still authenticated; waiting 60s to show the latent state")
    time.sleep(60)
    inc.poll_alerts()
    inc.mark("routine maintenance: PostgreSQL pod restarted (drops all connections)")
    kubectl("delete", "pod", "postgres-0", "-n", NS, "--wait=false", capture=True)


def s03_diagnose(inc: Incident):
    inc.diagnose("pods", "get", "pods", "-n", NS, "-o", "wide")
    inc.diagnose("endpoints", "get", "endpointslices", "-n", NS, "-l",
                 "kubernetes.io/service-name=datareliability-api", "-o", "wide")
    inc.diagnose("api_logs", "logs", "-n", NS, "-l", API_SELECTOR,
                 "--tail=6", "--prefix")
    inc.diagnose("postgres_logs", "logs", "postgres-0", "-n", NS, "--tail=10")
    inc.diagnose("backup_jobs", "get", "jobs", "-n", NS)


def s03_recover(inc: Incident):
    rotated = (STATE / "rotated_password").read_text()
    creds = credentials()
    inc.mark("remediation: roll forward - update Kubernetes Secret to the rotated password")
    apply_secret("postgres-credentials", {"username": creds["db_user"], "password": rotated})
    creds["db_password"] = rotated
    (STATE / "credentials.json").write_text(json.dumps(creds, indent=2))
    inc.mark("restart API pods so they read the new Secret value")
    kubectl("rollout", "restart", API_DEPLOY, "-n", NS, capture=True)
    kubectl("rollout", "status", API_DEPLOY, "-n", NS, ROLLOUT_TIMEOUT, capture=True)


# --- 04: bad release (SQL/schema mismatch passes readiness) -----------------
def s04_inject(inc: Incident):
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        for f in ("Dockerfile", "requirements.txt"):
            shutil.copy(ROOT / "app" / f, ctx / f)
        source = (ROOT / "app" / "main.py").read_text()
        # The "refactor" in release 1.1.0 renames a column in the INSERT only.
        bad = source.replace("(external_id, email, full_name, country)",
                             "(external_id, email, fullname, country)")
        assert bad != source
        (ctx / "main.py").write_text(bad)
        build_image("api", "1.1.0", ctx)
    inc.mark("release 1.1.0 deployed via helm upgrade")
    helm("upgrade", "api", str(ROOT / "charts" / "datareliability-api"), "-n", NS,
         "--reuse-values", "--set", "image.tag=1.1.0", "--wait", "--timeout", "5m", capture=True)
    inc.mark("rollout reported successful (all pods Ready)")


def s04_diagnose(inc: Incident):
    inc.diagnose("deployment", "get", "deploy", "datareliability-api", "-n", NS, "-o", "wide")
    inc.diagnostics["helm_history"] = out("helm", "--kube-context", CTX, "history", "api", "-n", NS)
    inc.diagnose("api_logs", "logs", "-n", NS, "-l", API_SELECTOR,
                 "--tail=5", "--prefix")
    inc.diagnostics["errors_by_route"] = json.dumps(prom_query(
        'sum by (route, method, status) (rate(http_requests_total{job="datareliability-api",status=~"5.."}[2m]))'),
        indent=1)


def s04_recover(inc: Incident):
    inc.mark("remediation: helm rollback to last known-good revision")
    helm("rollback", "api", "-n", NS, "--wait", "--timeout", "5m", capture=True)


# --- 05: worker node failure ------------------------------------------------
def s05_inject(inc: Incident):
    victim = kout("get", "pod", api_pods()[0], "-n", NS, "-o", "jsonpath={.spec.nodeName}")
    (STATE / "victim_node").write_text(victim)
    placement = kout("get", "pods", "-n", NS, "-l", API_SELECTOR,
                     "-o", "custom-columns=POD:.metadata.name,NODE:.spec.nodeName")
    inc.diagnostics["placement_before"] = placement
    inc.mark(f"node failure: docker stop {victim} (simulates instance loss)")
    sh("docker", "stop", victim, capture=True)


def s05_diagnose(inc: Incident):
    inc.diagnose("nodes", "get", "nodes")
    inc.diagnose("pods", "get", "pods", "-n", NS, "-o", "wide")
    victim = (STATE / "victim_node").read_text()
    inc.diagnose("node_taints", "get", "node", victim, "-o", "jsonpath={.spec.taints}")


def s05_watch_failover(inc: Incident) -> None:
    """Record when Kubernetes notices the node and when it replaces the pod."""
    victim = (STATE / "victim_node").read_text()
    seen_notready = seen_replaced = False
    start = time.monotonic()
    while time.monotonic() - start < 480 and not seen_replaced:
        inc.poll_alerts()
        ready = kout("get", "node", victim, "-o",
                     'jsonpath={.status.conditions[?(@.type=="Ready")].status}', check=False)
        if not seen_notready and ready != "True":
            seen_notready = True
            inc.mark(f"Kubernetes marked {victim} NotReady (Ready={ready})")
        pods = kout("get", "pods", "-n", NS, "-l", API_SELECTOR,
                    "-o", "jsonpath={range .items[*]}{.spec.nodeName} {.status.phase} "
                    "{.metadata.deletionTimestamp}{'\\n'}{end}")
        healthy_elsewhere = [ln for ln in pods.splitlines()
                             if ln.split()[:1] != [victim] and "Running" in ln and len(ln.split()) == 2]
        if seen_notready and len(healthy_elsewhere) >= 2:
            seen_replaced = True
            inc.mark("replacement pod Running on a healthy node (2 replicas restored)")
        time.sleep(5)
    if not seen_replaced:
        inc.mark("replacement not observed within 8 minutes")


def s05_recover(inc: Incident):
    victim = (STATE / "victim_node").read_text()
    inc.mark(f"restore capacity: docker start {victim}")
    sh("docker", "start", victim, capture=True)
    wait_for("node Ready", lambda: kout("get", "node", victim, "-o",
             'jsonpath={.status.conditions[?(@.type=="Ready")].status}') == "True", 300)
    inc.mark("node Ready again")


SCENARIOS = {
    "01": ("Pod crash loop after memory limit change", s01_inject, s01_diagnose, s01_recover,
           {"APIPodRestarting", "KubePodCrashLooping", "APIReplicasUnavailable"}),
    "02": ("Latency regression after index drop", s02_inject, s02_diagnose, s02_recover,
           {"APILatencyFastBurn", "APILatencySlowBurn", "APIRouteLatencyDegraded"}),
    "03": ("Database credential drift", s03_inject, s03_diagnose, s03_recover,
           {"SyntheticClientFailing", "APIDatabaseErrors", "APIAvailabilityFastBurn"}),
    "04": ("Bad release passes readiness", s04_inject, s04_diagnose, s04_recover,
           {"APIAvailabilityFastBurn", "SyntheticClientFailing"}),
    "05": ("Worker node failure", s05_inject, s05_diagnose, s05_recover,
           {"APIReplicasUnavailable", "SyntheticClientFailing", "KubeNodeNotReady"}),
}


def cmd_scenario(args: list[str]) -> None:
    if len(args) < 2 or args[0] not in SCENARIOS or args[1] not in ("run", "inject", "recover"):
        raise SystemExit(f"usage: lab.py scenario {{{','.join(SCENARIOS)}}} {{run|inject|recover}}")
    key, action = args[0], args[1]
    title, inject, diagnose, recover, expected = SCENARIOS[key]
    inc = Incident(scenario=f"scenario-{key}", title=title)
    if action == "inject":
        return inject(inc)
    if action == "recover":
        return recover(inc)

    hold = int(os.environ.get("LAB_HOLD_SECONDS", "120"))  # time "on call" before fixing
    probe = Probe()
    baseline(inc, probe)
    inject(inc)
    if key == "05":
        s05_watch_failover(inc)
    else:
        await_detection(inc, expected)
    time.sleep(10)
    inc.mark("diagnostics captured")
    diagnose(inc)
    if key != "05":
        # Hold the failure briefly, as a responder would while investigating,
        # so the alert timeline reflects a realistic response, not an instant fix.
        end = time.monotonic() + hold
        while time.monotonic() < end:
            inc.poll_alerts()
            time.sleep(5)
    recover(inc)
    await_recovery(inc, probe)
    time.sleep(5)
    probe.stop()
    inc.save(probe)
    print(json.dumps(probe.summary(inc.t0), indent=2))


# ===================================================================== DR drill
def run_restore_job(target_db: str, source_db: str = "customers") -> str:
    """Run backup/restore.sh as a Job: newest valid `source_db` backup -> `target_db`."""
    def secret(name: str, secret_name: str, key: str) -> dict:
        return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}}}

    job = f"postgres-restore-{int(time.time())}"
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": NS},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 3600, "template": {"spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "restore", "image": f"{IMAGES['backup'][0]}:{IMAGES['backup'][1]}",
                "command": ["restore.sh"],
                "envFrom": [{"configMapRef": {"name": "backup-config"}}],
                "env": [
                    {"name": "PGDATABASE", "value": target_db},
                    {"name": "BACKUP_SOURCE_DB", "value": source_db},
                    secret("PGUSER", "postgres-credentials", "username"),
                    secret("PGPASSWORD", "postgres-credentials", "password"),
                    secret("AWS_ACCESS_KEY_ID", "objectstore-credentials", "access_key"),
                    secret("AWS_SECRET_ACCESS_KEY", "objectstore-credentials", "secret_key"),
                ],
            }]}}},
    }
    kubectl("apply", "-f", "-", input=json.dumps(manifest), capture=True)
    done = kubectl("wait", "--for=condition=complete", f"job/{job}", "-n", NS, "--timeout=600s",
                   check=False, capture=True)
    log = kout("logs", f"job/{job}", "-n", NS, check=False)
    if done.returncode != 0:
        raise SystemExit(f"restore job {job} did not complete:\n{log}")
    return log


def cmd_verify_backup(_args: list[str]) -> dict:
    """Prove the newest backup restores: restore it into a scratch database
    and compare it with production. A backup that has never been restored
    is a hope, not a backup."""
    scratch = "restore_check"
    psql(f"DROP DATABASE IF EXISTS {scratch}")
    psql(f"CREATE DATABASE {scratch}")
    try:
        t0 = time.monotonic()
        log = run_restore_job(scratch)
        restore_s = time.monotonic() - t0
        key = json.loads(log.strip().splitlines()[-1])["key"]
        creds = credentials()

        def scratch_sql(sql: str) -> str:
            return kout("exec", "-n", NS, "postgres-0", "-c", "postgres", "--",
                        "env", f"PGPASSWORD={creds['db_password']}", "psql", "-U", creds["db_user"],
                        "-d", scratch, "-At", "-c", sql)

        restored = scratch_sql("SELECT count(*) || '|' || extract(epoch FROM now() - max(created_at))::int "
                               "FROM customers").split("|")
        indexes = scratch_sql("SELECT string_agg(indexname, ',' ORDER BY indexname) FROM pg_indexes "
                              "WHERE tablename = 'customers'")
        live = int(psql("SELECT count(*) FROM customers"))
        result = {"verified_at": iso(now()), "backup": key, "restore_seconds": round(restore_s, 1),
                  "rows_restored": int(restored[0]), "rows_live": live,
                  "newest_record_age_s": int(restored[1]), "indexes": indexes.split(",")}
        ok = result["rows_restored"] > 0 and "customers_email_idx" in result["indexes"]
        result["verdict"] = "PASS" if ok else "FAIL"
        say(f"backup verification {result['verdict']}: {json.dumps(result)}")
        EVIDENCE.mkdir(exist_ok=True)
        (EVIDENCE / f"backup-verify-{now():%Y%m%dT%H%M%SZ}.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        psql(f"DROP DATABASE IF EXISTS {scratch}", check=False)


def cmd_dr_drill(_args: list[str]) -> None:
    """Total loss of the database volume, restored from off-cluster backups.

    Targets are set here, before the drill runs: RTO 15 min, RPO 5 min.
    """
    rto_target, rpo_target = 15 * 60, 5 * 60
    inc = Incident(scenario="dr-drill", title="Database volume loss and restore from backup")
    probe = Probe()
    baseline(inc, probe)

    backups = s3("ls", f"s3://{BUCKET}/postgres/customers/").splitlines()
    if not backups:
        raise SystemExit("no backups yet: wait for the CronJob or run `lab.py backup-now`")
    latest = max(line.split()[-1] for line in backups)
    inc.diagnostics["backups_before"] = "\n".join(backups[-5:])
    before = http_json(f"{API_URL}/stats")[1]
    inc.mark("pre-disaster state recorded", latest_backup=latest, stats=before)

    # ---- disaster: the database and its volume are gone
    disaster = inc.mark("DISASTER: PostgreSQL StatefulSet and data volume deleted")
    kubectl("delete", "statefulset", "postgres", "-n", NS, "--wait=true", capture=True)
    kubectl("delete", "pvc", "data-postgres-0", "-n", NS, "--wait=true", capture=True)

    await_detection(inc, {"SyntheticClientFailing", "APIDatabaseErrors", "APIAvailabilityFastBurn"},
                    timeout=300)
    inc.diagnose("pods_during_outage", "get", "pods", "-n", NS, "-o", "wide")

    # ---- recovery, following docs/runbooks/disaster-recovery.md
    inc.mark("runbook step 1: stop writes - scale API to 0 so nothing writes to an empty database")
    kubectl("scale", API_DEPLOY, "-n", NS, "--replicas=0", capture=True)
    kubectl("scale", LOADGEN_DEPLOY, "-n", NS, "--replicas=0", capture=True)

    inc.mark("runbook step 2: recreate empty PostgreSQL on a new volume")
    kubectl("apply", "-f", str(ROOT / "k8s" / "postgres.yaml"), capture=True)
    kubectl("rollout", "status", "statefulset/postgres", "-n", NS, ROLLOUT_TIMEOUT, capture=True)
    wait_for("postgres accepting connections", lambda: psql("SELECT 1", check=False) == "1", 120)

    inc.mark("runbook step 3: restore newest valid backup from object storage", newest_listed=latest)
    inc.diagnostics["restore_log"] = run_restore_job("customers")
    inc.mark("restore job complete", log=inc.diagnostics["restore_log"].splitlines()[-1])

    # Measure recovery point now, before any new writes land: the newest
    # record that survived is the last write the backup captured.
    restored = psql("SELECT count(*) || '|' || coalesce(max(id), 0) || '|' || "
                    "extract(epoch FROM max(created_at)) FROM customers").split("|")
    newest = datetime.fromtimestamp(float(restored[2]), timezone.utc)
    inc.mark("restored data inspected", records=int(restored[0]), max_id=int(restored[1]),
             newest_record_at=iso(newest))

    inc.mark("runbook step 4: restart API and synthetic traffic")
    kubectl("scale", API_DEPLOY, "-n", NS, "--replicas=2", capture=True)
    kubectl("rollout", "restart", API_DEPLOY, "-n", NS, capture=True)
    kubectl("rollout", "status", API_DEPLOY, "-n", NS, ROLLOUT_TIMEOUT, capture=True)
    kubectl("scale", LOADGEN_DEPLOY, "-n", NS, "--replicas=1", capture=True)

    await_recovery(inc, probe)
    recovered = datetime.fromisoformat(inc.events[-1]["t"].replace("Z", "+00:00"))

    rto = (recovered - disaster).total_seconds()
    rpo = (disaster - newest).total_seconds()
    inc.diagnostics["result"] = json.dumps({
        "rto_target_s": rto_target, "rto_measured_s": round(rto, 1), "rto_met": rto <= rto_target,
        "rpo_target_s": rpo_target, "rpo_measured_s": round(rpo, 1), "rpo_met": rpo <= rpo_target,
        "records_before": before["customers"], "max_id_before": before["max_id"],
        "records_after_restore": int(restored[0]), "max_id_after_restore": int(restored[1]),
        "records_lost": before["max_id"] - int(restored[1]),
        "newest_record_after_restore": iso(newest),
        "restored_from": latest,
    }, indent=2)
    probe.stop()
    inc.save(probe)
    print(inc.diagnostics["result"])


# ========================================================================= main
COMMANDS = {
    "up": cmd_up, "deploy": cmd_deploy, "down": cmd_down, "seed": cmd_seed, "status": cmd_status,
    "backup-now": cmd_backup_now, "verify-backup": cmd_verify_backup, "scenario": cmd_scenario, "dr-drill": cmd_dr_drill,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
