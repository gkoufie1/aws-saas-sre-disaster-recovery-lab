"""Regression test for the 0-byte backup bug (found after scenario 03 / DR drill 001).

1. A backup that cannot authenticate must fail AND upload nothing.
2. With a 0-byte and a corrupt object planted as the *newest* backups, a
   restore must skip both and restore the newest valid archive.

    python scripts/test_backup_safety.py      (needs `lab.py up`)
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lab  # noqa: E402

PREFIX = f"s3://{lab.BUCKET}/postgres/customers/"


def keys() -> set[str]:
    return {line.split()[-1] for line in lab.s3("ls", PREFIX).splitlines()}


# --- 1: failing backup uploads nothing ---------------------------------------
before = keys()
job = f"backup-safety-{int(time.time())}"
manifest = json.loads(lab.kout("create", "job", job, "--from=cronjob/postgres-backup", "-n", lab.NS,
                               "--dry-run=client", "-o", "json"))
container = manifest["spec"]["template"]["spec"]["containers"][0]
container["env"] = [e for e in container["env"] if e["name"] != "PGPASSWORD"]
container["env"].append({"name": "PGPASSWORD", "value": "deliberately-wrong"})
manifest["spec"]["backoffLimit"] = 0
lab.kubectl("apply", "-f", "-", input=json.dumps(manifest), capture=True)
lab.kubectl("wait", "--for=condition=failed", f"job/{job}", "-n", lab.NS, "--timeout=120s", capture=True)
log = lab.kout("logs", f"job/{job}", "-n", lab.NS).strip().splitlines()[-1]
lab.kubectl("delete", "job", job, "-n", lab.NS, capture=True)
new = keys() - before
print(f"[1] failing backup log: {log}")
print(f"[1] objects uploaded by failing backup: {sorted(new) or 'none'}")
assert '"backup_failed"' in log and not new, "FAIL: a failed backup left an object behind"

# --- 2: restore skips empty and corrupt newest objects ------------------------
empty, corrupt = "29991231T235958Z.dump", "29991231T235959Z.dump"
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "empty").write_bytes(b"")
    (Path(tmp) / "corrupt").write_bytes(b"PGDMP this is not a real archive" * 100)
    lab.s3("cp", str(Path(tmp) / "empty"), PREFIX + empty)
    lab.s3("cp", str(Path(tmp) / "corrupt"), PREFIX + corrupt)
try:
    result = lab.cmd_verify_backup([])
    print(f"[2] restored from {result['backup']}: {result['verdict']}")
    assert result["verdict"] == "PASS"
    assert not result["backup"].startswith("postgres/customers/2999"), "FAIL: restored a planted bad object"
finally:
    lab.s3("rm", PREFIX + empty, check=False)
    lab.s3("rm", PREFIX + corrupt, check=False)
print("backup safety: PASS")
