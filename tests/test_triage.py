from conftest import load_sample
from email_parser import parse_email
from runbook_matcher import match
from severity import assess
import toy_cluster
from catalog import runbooks

EXPECTED = {
    "1-": ("batch-worker", "disk-pressure", "low"),
    "2-": ("search-service", "high-latency", "high"),
    "3-": ("checkout-service", "high-error-rate", "high"),
    "4-": ("payments-db", "db-connection-saturation", "critical"),
    "5-": ("orders-db", "disk-pressure", "high"),
}


def triage(prefix):
    email = load_sample(prefix)
    parsed = parse_email(email["sender"], email["subject"], email["body"])
    matched = match(parsed)
    state = toy_cluster.new_state()
    metrics = None
    if parsed["service"] in state:
        metrics = toy_cluster.metrics(state, parsed["service"])
        metrics["dependants"] = {n: toy_cluster.metrics(state, n) for n, s in state.items()
                                 if s.get("depends_on") == parsed["service"]}
    sev = assess(parsed, runbooks().get(matched["runbook_id"] or ""), metrics)
    return parsed, matched, sev


def test_samples_map_to_expected_runbook_and_severity():
    for prefix, (service, runbook, level) in EXPECTED.items():
        parsed, matched, sev = triage(prefix)
        assert parsed["service"] == service, prefix
        assert matched["runbook_id"] == runbook, (prefix, matched)
        assert sev["severity"] == level, (prefix, sev["reason"])


def test_unmatched_email_asks_for_llm_selection():
    parsed, matched, _ = triage("6-")
    assert matched["runbook_id"] is None and matched["needs_llm_selection"]
    assert matched["catalog"]


def test_low_confidence_jev_bumps_severity(monkeypatch):
    import severity
    monkeypatch.setattr(severity, "classify_severity", lambda s: {"severity": "low", "confidence": 0.4})
    _, _, sev = triage("1-")
    assert sev["severity"] == "high"
