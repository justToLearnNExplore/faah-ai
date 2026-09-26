from hardcoded_floor import check_hardcoded_floor
from router import route_action

UNDO = {"action_type": "scale", "params": {"replicas": 3}}
SAFE_META = {"service": "search-service", "stateful": False, "category": "compute", "undo": UNDO}


def test_floor_blocks_disguised_destruction():
    tricks = [
        ("tidy up old WAL segments", "run_shell", "rm  -rf /var/lib/postgresql/data/pg_wal/*"),
        ("routine housekeeping: set replicas: 0 for legacy-orders", "scale", None),
        ("quick revert of last deploy", "rollback", None),
        ("clean up table", "run_shell", "psql -c 'TRUNCATE orders'"),
    ]
    for description, action_type, command in tricks:
        assert check_hardcoded_floor(description, action_type, command)["floor_triggered"], description


def test_floor_allows_plain_scale_out():
    assert not check_hardcoded_floor("scale search-service replicas to 8", "scale")["floor_triggered"]
    assert not check_hardcoded_floor("scale search-service replicas to 10", "scale")["floor_triggered"]


def test_safe_step_auto_executes_on_high():
    r = route_action("ctx", "scale search-service replicas to 8", "scale", SAFE_META, "high")
    assert r["decision"] == "auto_execute", r["reasons"]


def test_critical_never_auto_executes():
    r = route_action("ctx", "scale search-service replicas to 8", "scale", SAFE_META, "critical")
    assert r["decision"] == "needs_approval"
    assert any("CRITICAL" in reason for reason in r["reasons"])


def test_floor_wins_over_confident_jev(monkeypatch):
    import router
    monkeypatch.setattr(router, "classify_action", lambda *a: {
        "reversibility": "reversible", "reversibility_confidence": 0.99, "risk_score": 0.01,
        "route": "auto_execute", "route_confidence": 0.99, "reason": "tricked"})
    r = route_action("ctx", "rollback checkout-service to v2.3.0", "rollback",
                     {**SAFE_META, "category": "compute"}, "low")
    assert r["decision"] == "needs_approval"
    assert r["source"] == "hardcoded_floor"


def test_jev_thresholds_are_severity_aware(monkeypatch):
    import router
    monkeypatch.setattr(router, "classify_action", lambda *a: {
        "reversibility": "reversible", "reversibility_confidence": 0.9, "risk_score": 0.25,
        "route": "auto_execute", "route_confidence": 0.9, "reason": "x"})
    assert route_action("ctx", "scale x to 8", "scale", SAFE_META, "low")["decision"] == "auto_execute"
    assert route_action("ctx", "scale x to 8", "scale", SAFE_META, "high")["decision"] == "needs_approval"


def test_jev_outage_fails_closed(monkeypatch):
    import router
    from jev_classifier import _fail_closed
    monkeypatch.setattr(router, "classify_action", lambda *a: _fail_closed(RuntimeError("down"), 0.0))
    r = route_action("ctx", "scale x to 8", "scale", SAFE_META, "low")
    assert r["decision"] == "needs_approval"


def test_stateful_and_missing_undo_need_approval():
    assert route_action("ctx", "scale x to 8", "scale", {**SAFE_META, "stateful": True}, "low")["decision"] == "needs_approval"
    assert route_action("ctx", "scale x to 8", "scale", {**SAFE_META, "undo": None}, "low")["decision"] == "needs_approval"
