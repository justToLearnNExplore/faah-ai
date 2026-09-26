import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import incidents  # noqa: E402
import router  # noqa: E402
import severity  # noqa: E402


def fake_jev_action(incident_context, proposed_action, action_metadata):
    """Deterministic stand-in for Jev so tests don't call the API."""
    category = action_metadata.get("category")
    safe = category == "compute" and not action_metadata.get("stateful")
    return {
        "reversibility": "reversible" if safe else "irreversible",
        "reversibility_confidence": 0.95,
        "risk_score": 0.08 if safe else 0.8,
        "risk_confidence": 0.9,
        "route": "auto_execute" if safe else "human_review",
        "route_confidence": 0.93,
        "reason": "fake jev",
    }


def fake_jev_severity(signals):
    return {"severity": "low", "confidence": 0.9}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "DATA_DIR", tmp_path)
    monkeypatch.setattr(incidents, "INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setattr(incidents, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(incidents, "CLUSTER_FILE", tmp_path / "cluster.json")
    monkeypatch.setattr(router, "classify_action", fake_jev_action)
    monkeypatch.setattr(severity, "classify_severity", fake_jev_severity)
    yield


def load_sample(name):
    path = next((ROOT / "samples").glob(f"{name}*.json"))
    return json.loads(path.read_text())
