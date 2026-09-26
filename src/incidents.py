"""
Incident state + audit trail, persisted as JSON under data/.

The MCP server is the single writer. The orchestrator reads incidents through
the /internal HTTP routes to render the dashboard.
"""

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import toy_cluster
from catalog import ROOT

DATA_DIR = ROOT / "data"
INCIDENT_DIR = DATA_DIR / "incidents"
AUDIT_DIR = DATA_DIR / "audit"
CLUSTER_FILE = DATA_DIR / "cluster.json"

_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


# ---------------------------------------------------------------- cluster

def cluster() -> dict:
    with _lock:
        if not CLUSTER_FILE.exists():
            _write(CLUSTER_FILE, toy_cluster.new_state())
        return json.loads(CLUSTER_FILE.read_text())


def save_cluster(state: dict) -> None:
    with _lock:
        _write(CLUSTER_FILE, state)


def reset_cluster() -> dict:
    state = toy_cluster.new_state()
    save_cluster(state)
    return state


# ---------------------------------------------------------------- incidents

def create(sender: str, subject: str, body: str, received_via: str) -> dict:
    with _lock:
        incident_id = f"INC-{datetime.now(timezone.utc).strftime('%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        incident = {
            "id": incident_id,
            "created_at": now(),
            "received_via": received_via,
            "email": {"sender": sender, "subject": subject, "body": body},
            "status": "received",
            "parsed": None,
            "runbook_match": None,
            "runbook_id": None,
            "severity": None,
            "investigation": [],
            "plan": None,
            "sandbox": None,
            "approvals": {},
            "executions": [],
            "verification": None,
            "resolution": None,
        }
        _write(INCIDENT_DIR / f"{incident_id}.json", incident)
        audit(incident_id, "incident.received", {"sender": sender, "subject": subject, "via": received_via})
        return incident


def get(incident_id: str) -> dict:
    path = INCIDENT_DIR / f"{incident_id}.json"
    if not path.exists():
        raise KeyError(f"unknown incident {incident_id!r}")
    with _lock:
        return json.loads(path.read_text())


def save(incident: dict) -> dict:
    with _lock:
        _write(INCIDENT_DIR / f"{incident['id']}.json", incident)
    return incident


def update(incident_id: str, **fields) -> dict:
    with _lock:
        incident = get(incident_id)
        incident.update(fields)
        return save(incident)


def list_all() -> list[dict]:
    if not INCIDENT_DIR.exists():
        return []
    items = []
    for path in sorted(INCIDENT_DIR.glob("*.json"), reverse=True):
        inc = json.loads(path.read_text())
        items.append({
            "id": inc["id"],
            "created_at": inc["created_at"],
            "subject": inc["email"]["subject"],
            "service": (inc.get("parsed") or {}).get("service"),
            "severity": (inc.get("severity") or {}).get("severity"),
            "status": inc["status"],
        })
    return items


def find_step(incident: dict, step_id: str) -> dict:
    for step in (incident.get("plan") or {}).get("steps", []):
        if step["step_id"] == step_id:
            return step
    raise KeyError(f"no step {step_id!r} in the plan for {incident['id']}")


# ---------------------------------------------------------------- audit

def audit(incident_id: str, event: str, data: dict | None = None) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": now(), "incident_id": incident_id, "event": event, "data": data or {}}, default=str)
    with _lock, open(AUDIT_DIR / f"{incident_id}.jsonl", "a") as f:
        f.write(line + "\n")


def audit_log(incident_id: str) -> list[dict]:
    path = AUDIT_DIR / f"{incident_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
