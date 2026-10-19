#!/usr/bin/env python3
"""DataReliability on AWS (Milestone 2): EKS + RDS PostgreSQL + S3.

Reuses lab.py's probe, alert tracking and evidence recorder, pointed at EKS
through kubectl port-forwards instead of kind NodePorts.

    terraform -chdir=terraform apply             # infrastructure (see README)
    python lab_aws.py deploy                     # images -> ECR, monitoring, API, backups, traffic
    python lab_aws.py seed [ROWS]
    python lab_aws.py status
    python lab_aws.py pitr-drill                 # DR Drill 002: bad DELETE -> RDS point-in-time restore
    python lab_aws.py forward                    # keep port-forwards open for screenshots
    python lab_aws.py cleanup-restored           # delete RDS instances created by drills (not in Terraform)

Ports (chosen to not collide with the local kind lab):
    API 127.0.0.1:31080 · Prometheus 127.0.0.1:31090 · Grafana 127.0.0.1:31300
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import lab

TF_DIR = lab.ROOT / "terraform"
CTX = "datareliability-lab"
FORWARDS = {  # local port -> (namespace, service, service port)
    31080: (lab.NS, "datareliability-api", 80),
    31090: (lab.MON_NS, "kube-prometheus-stack-prometheus", 9090),
    31300: (lab.MON_NS, "kube-prometheus-stack-grafana", 80),
}

# Retarget lab.py's helpers at EKS.
lab.CTX = CTX
lab.API_URL = "http://127.0.0.1:31080"
lab.PROM_URL = "http://127.0.0.1:31090"


def tf_outputs() -> dict:
    raw = lab.out("terraform", f"-chdir={TF_DIR}", "output", "-json")
    return {k: v["value"] for k, v in json.loads(raw).items()}


def aws(*args: str, check: bool = True) -> str:
    return lab.out("aws", "--region", tf_outputs_cached()["region"], *args, check=check)


_outputs: dict | None = None


def tf_outputs_cached() -> dict:
    global _outputs
    if _outputs is None:
        _outputs = tf_outputs()
    return _outputs


def aws_json(*args: str):
    return json.loads(aws(*args, "--output", "json"))


# ------------------------------------------------------------- port-forwards
class Forwarder(threading.Thread):
    """kubectl port-forward that restarts itself.

    A Service port-forward binds to one pod; when that pod is replaced
    (rollout, restart) the forward dies. This loop re-establishes it within
    about a second. Gaps show up in the probe as connection errors, so
    drills note that the probe path includes the port-forward.
    """

    def __init__(self, port: int, ns: str, svc: str, svc_port: int):
        super().__init__(daemon=True)
        self.args = ["kubectl", "--context", CTX, "-n", ns, "port-forward", f"svc/{svc}",
                     f"{port}:{svc_port}", "--address", "127.0.0.1"]

    def run(self):
        while True:
            subprocess.run(self.args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)


def start_forwards(only: tuple[int, ...] | None = None) -> None:
    for port, (ns, svc, svc_port) in FORWARDS.items():
        if only is None or port in only:
            Forwarder(port, ns, svc, svc_port).start()
    time.sleep(4)


# ------------------------------------------------------------------- toolbox
TOOLBOX = "toolbox"


def ensure_toolbox(creds: dict, host: str) -> None:
    """A long-lived psql pod: RDS is private, so SQL runs from inside the VPC."""
    if lab.kout("get", "pod", TOOLBOX, "-n", lab.NS, "--ignore-not-found", "-o", "name"):
        return
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": TOOLBOX, "namespace": lab.NS},
        "spec": {"containers": [{
            "name": "psql", "image": "postgres:16-alpine", "command": ["sleep", "infinity"],
            "env": [{"name": "PGHOST", "value": host}, {"name": "PGUSER", "value": creds["username"]},
                    {"name": "PGPASSWORD", "value": creds["password"]},
                    {"name": "PGDATABASE", "value": "customers"}, {"name": "PGSSLMODE", "value": "require"}],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
        }]},
    }
    lab.kubectl("apply", "-f", "-", input=json.dumps(pod), capture=True)
    lab.kubectl("wait", "--for=condition=Ready", f"pod/{TOOLBOX}", "-n", lab.NS, "--timeout=180s", capture=True)


def psql_on(host: str | None, sql: str, check: bool = True) -> str:
    args = ["exec", "-n", lab.NS, TOOLBOX, "--", "psql", "-At", "-v", "ON_ERROR_STOP=1"]
    if host:
        args += ["-h", host]
    return lab.kout(*args, "-c", sql, check=check)


def aws_psql(sql: str, check: bool = True) -> str:
    return psql_on(None, sql, check)


lab.psql = aws_psql  # lab.cmd_seed and friends now run SQL against RDS


def db_credentials() -> dict:
    """RDS-managed master credentials, read from Secrets Manager."""
    secret = aws_json("secretsmanager", "get-secret-value",
                      "--secret-id", tf_outputs_cached()["db_master_secret_arn"])
    return json.loads(secret["SecretString"])


# -------------------------------------------------------------------- deploy
def ecr_push(registry: str) -> dict[str, str]:
    password = aws("ecr", "get-login-password")
    lab.sh("docker", "login", "--username", "AWS", "--password-stdin", registry, input=password, capture=True)
    refs = {}
    for key, (name, tag, _ctx) in lab.IMAGES.items():
        remote = f"{registry}/{name}:{tag}"
        exists = aws("ecr", "describe-images", "--repository-name", name, "--image-ids",
                     f"imageTag={tag}", "--query", "imageDetails[0].imageTags", check=False)
        if tag not in exists:  # ECR tags are immutable: push each version once
            lab.say(f"pushing {remote}")
            lab.sh("docker", "tag", f"{name}:{tag}", remote)
            lab.sh("docker", "push", "-q", remote, capture=True)
        refs[key] = remote
    return refs


def apply_json(obj: dict) -> None:
    lab.kubectl("apply", "-f", "-", input=json.dumps(obj), capture=True)


def cmd_deploy(_args: list[str]) -> None:
    o = tf_outputs_cached()
    lab.say(f"kubeconfig for {o['cluster_name']} (context {CTX})")
    lab.sh("aws", "eks", "update-kubeconfig", "--region", o["region"], "--name", o["cluster_name"],
           "--alias", CTX, capture=True)
    lab.say(lab.kout("get", "nodes", "-o", "wide"))

    registry = o["ecr_repositories"]["datareliability-api"].split("/")[0]
    refs = ecr_push(registry)

    creds = db_credentials()
    for ns in (lab.NS, lab.MON_NS):
        lab.kubectl("create", "namespace", ns, check=False, capture=True)
    lab.apply_secret("postgres-credentials", {"username": creds["username"], "password": creds["password"]})

    lab.say(f"installing kube-prometheus-stack {lab.KPS_CHART_VERSION}")
    lab.sh("helm", "repo", "add", "prometheus-community",
           "https://prometheus-community.github.io/helm-charts", check=False, capture=True)
    grafana_pw = lab.credentials()["grafana_password"]
    lab.helm("upgrade", "--install", "kube-prometheus-stack", "prometheus-community/kube-prometheus-stack",
             "--version", lab.KPS_CHART_VERSION, "-n", lab.MON_NS,
             "-f", str(lab.ROOT / "observability" / "kube-prometheus-stack-values.yaml"),
             "-f", str(lab.ROOT / "observability" / "kube-prometheus-stack-values.aws.yaml"),
             "--set", f"grafana.adminPassword={grafana_pw}",
             "--wait", "--timeout", "10m", capture=True)
    lab.helm("upgrade", "--install", "observability", str(lab.ROOT / "charts" / "datareliability-observability"),
             "-n", lab.MON_NS, capture=True)

    lab.say(f"deploying API -> RDS {o['db_endpoint']}")
    api_repo, api_tag = refs["api"].rsplit(":", 1)
    lab.helm("upgrade", "--install", "api", str(lab.ROOT / "charts" / "datareliability-api"), "-n", lab.NS,
             "--set", f"image.repository={api_repo}", "--set", f"image.tag={api_tag}",
             "--set", f"database.host={o['db_endpoint']}", "--set", "service.type=ClusterIP",
             "--wait", "--timeout", "5m", capture=True)

    deploy_backup(refs["backup"], o)
    deploy_loadgen(refs["loadgen"])
    ensure_toolbox(creds, o["db_endpoint"])
    lab.say("deployed. `python lab_aws.py seed` next")


def deploy_backup(image: str, o: dict) -> None:
    """Same CronJob as locally, but S3 via Pod Identity: no static keys."""
    apply_json({"apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "postgres-backup", "namespace": lab.NS}})
    apply_json({"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": "backup-config", "namespace": lab.NS},
                "data": {"PGHOST": o["db_endpoint"], "PGPORT": "5432", "PGDATABASE": "customers",
                         "PGSSLMODE": "require", "BACKUP_BUCKET": o["backup_bucket"],
                         "AWS_DEFAULT_REGION": o["region"]}})
    env = [{"name": n, "valueFrom": {"secretKeyRef": {"name": "postgres-credentials", "key": k}}}
           for n, k in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    apply_json({
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "postgres-backup", "namespace": lab.NS},
        "spec": {"schedule": "*/5 * * * *", "concurrencyPolicy": "Forbid",
                 "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
                 "jobTemplate": {"spec": {"backoffLimit": 1, "activeDeadlineSeconds": 240, "template": {
                     "metadata": {"labels": {"app.kubernetes.io/name": "postgres-backup"}},
                     "spec": {"restartPolicy": "Never", "serviceAccountName": "postgres-backup",
                              "containers": [{"name": "backup", "image": image, "command": ["backup.sh"],
                                              "envFrom": [{"configMapRef": {"name": "backup-config"}}],
                                              "env": env,
                                              "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                            "limits": {"memory": "512Mi"}}}]}}}}},
    })


def deploy_loadgen(image: str) -> None:
    labels = {"app.kubernetes.io/name": "loadgen"}
    apply_json({"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "loadgen", "namespace": lab.NS, "labels": labels},
                "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {
                    "metadata": {"labels": labels},
                    "spec": {"containers": [{
                        "name": "loadgen", "image": image,
                        "env": [{"name": "TARGET_URL", "value": "http://datareliability-api"},
                                {"name": "RPS", "value": "10"}],
                        "ports": [{"containerPort": 9100, "name": "metrics"}],
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"memory": "128Mi"}}}]}}}})
    apply_json({"apiVersion": "v1", "kind": "Service",
                "metadata": {"name": "loadgen", "namespace": lab.NS, "labels": labels},
                "spec": {"selector": labels, "ports": [{"name": "metrics", "port": 9100}]}})
    apply_json({"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
                "metadata": {"name": "loadgen", "namespace": lab.NS,
                             "labels": {"release": "kube-prometheus-stack"}},
                "spec": {"selector": {"matchLabels": labels},
                         "endpoints": [{"port": "metrics", "interval": "15s"}]}})


def cmd_seed(args: list[str]) -> None:
    ensure_toolbox(db_credentials(), tf_outputs_cached()["db_endpoint"])
    lab.cmd_seed(args)


def cmd_status(_args: list[str]) -> None:
    o = tf_outputs_cached()
    print(lab.kout("get", "nodes", "-L", "topology.kubernetes.io/zone,eks.amazonaws.com/capacityType"))
    print(lab.kout("get", "pods", "-n", lab.NS, "-o", "wide"))
    for db in aws_json("rds", "describe-db-instances")["DBInstances"]:
        if db["DBInstanceIdentifier"].startswith("datareliability"):
            print(f"RDS {db['DBInstanceIdentifier']}: {db['DBInstanceStatus']} multi_az={db['MultiAZ']} "
                  f"latest_restorable={db.get('LatestRestorableTime')}")
    print(aws("s3", "ls", f"s3://{o['backup_bucket']}/postgres/customers/", check=False) or "no backups yet")


def cmd_forward(_args: list[str]) -> None:
    start_forwards()
    print("API http://127.0.0.1:31080/docs · Prometheus http://127.0.0.1:31090 · "
          "Grafana http://127.0.0.1:31300 (admin / .lab/credentials.json grafana_password). Ctrl+C to stop.")
    while True:
        time.sleep(3600)


# ------------------------------------------------------------- DR Drill 002
def wait_restorable(db_id: str, target: datetime, inc: lab.Incident) -> float:
    """PITR can only restore up to LatestRestorableTime, which lags real time."""
    start = time.monotonic()
    while True:
        db = aws_json("rds", "describe-db-instances", "--db-instance-identifier", db_id)["DBInstances"][0]
        latest = datetime.fromisoformat(db["LatestRestorableTime"])
        if latest >= target:
            waited = time.monotonic() - start
            inc.mark("restore point is now restorable", latest_restorable=lab.iso(latest),
                     waited_s=round(waited, 1))
            return waited
        time.sleep(15)


def cmd_pitr_drill(_args: list[str]) -> None:
    """Logical corruption, not infrastructure loss: a mass DELETE commits on a
    healthy database. Recover with RDS point-in-time restore to just before it.

    Targets (set before running): RTO 60 min (PITR creates a new instance,
    typically 10-25 min), RPO 0 for data before the bad change.
    """
    o = tf_outputs_cached()
    src = o["db_identifier"]
    creds = db_credentials()
    ensure_toolbox(creds, o["db_endpoint"])
    start_forwards((31080, 31090))

    inc = lab.Incident(scenario="dr-drill-002-pitr", title="Accidental mass DELETE recovered with RDS PITR")
    probe = lab.Probe()
    lab.baseline(inc, probe)

    before = psql_on(None, "SELECT count(*) || '|' || count(*) FILTER (WHERE country = 'GB') FROM customers").split("|")
    inc.mark("pre-incident state", rows=int(before[0]), gb_rows=int(before[1]))

    # ---- the bad change, timestamped by the database's own clock
    out = psql_on(None, "WITH d AS (DELETE FROM customers WHERE country = 'GB' RETURNING 1) "
                        "SELECT count(*) || '|' || to_char(clock_timestamp() AT TIME ZONE 'UTC', "
                        "'YYYY-MM-DD\"T\"HH24:MI:SS.US') FROM d").split("|")
    deleted, bad_at = int(out[0]), datetime.fromisoformat(out[1] + "+00:00")
    inc.mark("BAD CHANGE: DELETE FROM customers WHERE country = 'GB' committed",
             rows_deleted=deleted, db_clock=lab.iso(bad_at))
    time.sleep(30)
    inc.poll_alerts()
    inc.mark("availability SLO unaffected: the service is up, the data is wrong",
             firing=sorted(inc._firing))

    # ---- restore to 1 second before the bad change
    target = bad_at - timedelta(seconds=1)
    inc.mark("decision: point-in-time restore to 1 s before the DELETE", restore_time=lab.iso(target))
    wait_restorable(src, target, inc)
    new_id = f"{src}-pitr-{datetime.now(timezone.utc):%H%M%S}"
    src_db = aws_json("rds", "describe-db-instances", "--db-instance-identifier", src)["DBInstances"][0]
    aws("rds", "restore-db-instance-to-point-in-time",
        "--source-db-instance-identifier", src, "--target-db-instance-identifier", new_id,
        "--restore-time", target.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "--db-instance-class", src_db["DBInstanceClass"],
        "--db-subnet-group-name", src_db["DBSubnetGroup"]["DBSubnetGroupName"],
        "--vpc-security-group-ids", *[g["VpcSecurityGroupId"] for g in src_db["VpcSecurityGroups"]],
        "--db-parameter-group-name", src_db["DBParameterGroups"][0]["DBParameterGroupName"],
        "--no-publicly-accessible", "--no-deletion-protection", "--copy-tags-to-snapshot",
        "--tags", "Key=Project,Value=datareliability", "Key=CreatedBy,Value=pitr-drill",
        "--query", "DBInstance.DBInstanceIdentifier")
    inc.mark("restore-db-instance-to-point-in-time issued", new_instance=new_id)
    t_restore = time.monotonic()
    while True:
        db = aws_json("rds", "describe-db-instances", "--db-instance-identifier", new_id)["DBInstances"][0]
        if db["DBInstanceStatus"] == "available":
            break
        inc.poll_alerts()
        time.sleep(20)
    new_host = db["Endpoint"]["Address"]
    inc.mark("restored instance available", seconds=round(time.monotonic() - t_restore), endpoint=new_host)

    # ---- verify the restored data before cutting over
    restored = psql_on(new_host, "SELECT count(*) || '|' || count(*) FILTER (WHERE country = 'GB') || '|' || "
                                 "coalesce(max(id), 0) FROM customers").split("|")
    inc.mark("restored data verified", rows=int(restored[0]), gb_rows=int(restored[1]), max_id=int(restored[2]))

    # ---- cutover: stop writes, carry over good writes made after the restore point
    inc.mark("cutover step 1: stop writes (API + loadgen to 0)")
    lab.kubectl("scale", lab.API_DEPLOY, "-n", lab.NS, "--replicas=0", capture=True)
    lab.kubectl("scale", lab.LOADGEN_DEPLOY, "-n", lab.NS, "--replicas=0", capture=True)
    lab.kubectl("wait", "--for=delete", "pod", "-l", lab.API_SELECTOR, "-n", lab.NS, "--timeout=120s",
                check=False, capture=True)

    max_restored_id = int(restored[2])
    cutoff = f"'{target.isoformat()}'"
    inserted_after = int(psql_on(None, f"SELECT count(*) FROM customers WHERE id > {max_restored_id}"))
    updated_after = int(psql_on(None, f"SELECT count(*) FROM customers WHERE id <= {max_restored_id} "
                                      f"AND updated_at > {cutoff}"))
    inc.mark("writes on the old instance after the restore point", inserted=inserted_after,
             updated_existing=updated_after)
    # Copy rows created after the restore point (good writes, not part of the
    # bad change) from the old instance into the restored one.
    copy = ("psql -At -v ON_ERROR_STOP=1 -c \"\\copy (SELECT * FROM customers WHERE id > "
            f"{max_restored_id} ORDER BY id) TO STDOUT\" | psql -h {new_host} -v ON_ERROR_STOP=1 -c "
            "\"\\copy customers FROM STDIN\" && psql -h " + new_host + " -At -c "
            "\"SELECT setval('customers_id_seq', (SELECT max(id) FROM customers))\"")
    lab.kout("exec", "-n", lab.NS, TOOLBOX, "--", "sh", "-c", copy)
    inc.mark("cutover step 2: post-restore-point inserts reconciled into restored instance",
             rows=inserted_after, lost_updates=updated_after)

    inc.mark("cutover step 3: repoint API and backups to the restored instance")
    lab.helm("upgrade", "api", str(lab.ROOT / "charts" / "datareliability-api"), "-n", lab.NS,
             "--reuse-values", "--set", f"database.host={new_host}", "--set", "replicaCount=2",
             "--wait", "--timeout", "5m", capture=True)
    lab.kubectl("patch", "configmap", "backup-config", "-n", lab.NS, "--type", "merge",
                "-p", json.dumps({"data": {"PGHOST": new_host}}), capture=True)
    lab.kubectl("scale", lab.LOADGEN_DEPLOY, "-n", lab.NS, "--replicas=1", capture=True)
    lab.kubectl("delete", "pod", TOOLBOX, "-n", lab.NS, "--wait=true", capture=True)
    ensure_toolbox(creds, new_host)

    lab.await_recovery(inc, probe)
    final = psql_on(None, "SELECT count(*) || '|' || count(*) FILTER (WHERE country = 'GB') FROM customers").split("|")
    recovered_at = datetime.fromisoformat(inc.events[-1]["t"].replace("Z", "+00:00"))
    inc.diagnostics["result"] = json.dumps({
        "restore_point": lab.iso(target), "bad_change_at": lab.iso(bad_at),
        "rows_deleted_by_bad_change": deleted,
        "gb_rows_before": int(before[1]), "gb_rows_after_recovery": int(final[1]),
        "rows_before": int(before[0]), "rows_after_recovery": int(final[0]),
        "post_restore_point_inserts_reconciled": inserted_after,
        "post_restore_point_updates_lost": updated_after,
        "rto_s_bad_change_to_recovered": round((recovered_at - bad_at).total_seconds(), 1),
        "old_instance_kept_for_investigation": src, "new_primary": new_id,
    }, indent=2)
    probe.stop()
    inc.save(probe)
    print(inc.diagnostics["result"])


def cmd_cleanup_restored(_args: list[str]) -> None:
    """Drill-created RDS instances are outside Terraform state; delete them
    explicitly before `terraform destroy`, or they keep billing."""
    for db in aws_json("rds", "describe-db-instances")["DBInstances"]:
        if "-pitr-" in db["DBInstanceIdentifier"]:
            lab.say(f"deleting {db['DBInstanceIdentifier']}")
            aws("rds", "delete-db-instance", "--db-instance-identifier", db["DBInstanceIdentifier"],
                "--skip-final-snapshot", "--delete-automated-backups", "--query", "DBInstance.DBInstanceStatus")


COMMANDS = {"deploy": cmd_deploy, "seed": cmd_seed, "status": cmd_status, "forward": cmd_forward,
            "pitr-drill": cmd_pitr_drill, "cleanup-restored": cmd_cleanup_restored}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
