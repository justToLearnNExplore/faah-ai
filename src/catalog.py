"""Loads the runbook catalog (runbooks/*.yaml) and the service catalog (config/services.yaml)."""

from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK_DIR = ROOT / "runbooks"
SERVICES_FILE = ROOT / "config" / "services.yaml"

GENERIC_RUNBOOK_ID = "generic-triage"


@lru_cache(maxsize=1)
def runbooks() -> dict[str, dict]:
    books = {}
    for path in sorted(RUNBOOK_DIR.glob("*.yaml")):
        book = yaml.safe_load(path.read_text())
        books[book["id"]] = book
    return books


@lru_cache(maxsize=1)
def services() -> dict[str, dict]:
    return yaml.safe_load(SERVICES_FILE.read_text())["services"]


def service_info(name: str | None) -> dict:
    """Catalog entry for a service; unknown services are treated as tier-1 and stateful (cautious default)."""
    return services().get(name or "", {"tier": 1, "customer_facing": True, "stateful": True, "owner": "unknown"})


def runbook_summary(book: dict) -> dict:
    return {
        "id": book["id"],
        "name": book["name"],
        "description": book["description"],
        "default_severity": book.get("default_severity"),
        "remediation_actions": [s["action_type"] for s in book["steps"] if s["kind"] == "remediate"],
    }
