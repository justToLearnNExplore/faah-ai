"""
Remediation plan builder (PLAN.md sections 4 + 5).

Takes the steps the agent drafted, validates them against the runbook and the
action registry, scores every one (floor -> Jev -> severity thresholds ->
blast radius -> undo) and computes the overall plan risk. Also builds the
sandbox test script and verifies what the sandbox reports.
"""

import ast
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import incidents
import toy_cluster
from catalog import runbooks, service_info
from router import THRESHOLDS, route_action

SANDBOX_MARKER = "FAAH_SANDBOX_RESULT "
LEVELS = ["low", "high", "critical"]


def _risk_label(risk: float) -> str:
    return "low" if risk < 0.3 else "high" if risk < 0.7 else "critical"


def _incident_context(incident: dict) -> str:
    parsed = incident.get("parsed") or {}
    sev = (incident.get("severity") or {}).get("severity")
    book = runbooks().get(incident.get("runbook_id") or "", {})
    return (
        f"{parsed.get('service')}: {parsed.get('subject')} | runbook: {book.get('name')} | "
        f"severity: {sev} | diagnosis: {(incident.get('plan') or {}).get('diagnosis', '')}"
    )


def build_plan(incident: dict, diagnosis: str, drafted_steps: list[dict]) -> dict:
    severity = (incident.get("severity") or {}).get("severity") or "high"
    state = incidents.cluster()
    book = runbooks().get(incident.get("runbook_id") or "", {})
    allowed = {s["action_type"] for s in book.get("steps", []) if s["kind"] == "remediate"}
    if allowed:
        allowed.add("run_shell")  # manual escape hatch; the floor always escalates it
    context = f"{_incident_context(incident)} {diagnosis}"

    steps = []
    for i, raw in enumerate(drafted_steps, start=1):
        action_type = raw.get("action_type", "")
        service = raw.get("service") or (incident.get("parsed") or {}).get("service")
        params = raw.get("params") or {}
        meta = toy_cluster.ACTIONS.get(action_type)
        problems = []
        if meta is None:
            problems.append(f"unknown action_type {action_type!r}")
        if service not in state:
            problems.append(f"unknown service {service!r}")
        if meta and not meta.get("read_only") and action_type not in allowed:
            problems.append(f"{action_type!r} is not a remediation step in runbook {book.get('id')!r}")
        for name in (meta or {}).get("params", []):
            if name not in params:
                problems.append(f"missing param {name!r}")

        description = toy_cluster.describe_action(action_type, service, params)
        info = service_info(service)
        steps.append({
            "step_id": f"S{i}",
            "title": raw.get("title") or description,
            "action_type": action_type,
            "service": service,
            "params": params,
            "command": params.get("command"),
            "description": description,
            "agent_description": raw.get("description"),
            "rationale": raw.get("rationale"),
            "evidence": raw.get("evidence"),
            "expected_outcome": raw.get("expected_outcome"),
            "category": (meta or {}).get("category", "unknown"),
            "stateful": info.get("stateful"),
            "undo": toy_cluster.undo_for(state, action_type, service, params) if meta else None,
            "problems": problems,
            "sandbox": None,
            "execution": None,
        })

    def score(step):
        if step["problems"]:
            return {"decision": "invalid", "source": "validation", "reasons": step["problems"],
                    "floor": None, "jev": None, "thresholds": None}
        metadata = {
            "service": step["service"],
            "env": (incident.get("parsed") or {}).get("env"),
            "tier": service_info(step["service"]).get("tier"),
            "stateful": step["stateful"],
            "category": step["category"],
            "undo": step["undo"],
            "agent_rationale": step["rationale"],
        }
        # Score the canonical description AND whatever the agent wrote, so a
        # benign-sounding paraphrase can't hide the real action from the floor.
        text = step["description"]
        if step["agent_description"] and step["agent_description"] != text:
            text = f"{text} || agent says: {step['agent_description']}"
        return route_action(context, text, step["action_type"], metadata, severity, step["command"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        routings = list(pool.map(score, steps))

    for step, routing in zip(steps, routings):
        step["routing"] = routing
        step["decision"] = routing["decision"]

    scored = [s["routing"]["jev"]["risk_score"] for s in steps if s["routing"].get("jev")]
    max_risk = max(scored, default=0.0)
    label = max(_risk_label(max_risk), severity, key=LEVELS.index)
    counts = {
        "auto_execute": sum(s["decision"] == "auto_execute" for s in steps),
        "needs_approval": sum(s["decision"] == "needs_approval" for s in steps),
        "read_only": sum(s["decision"] == "read_only" for s in steps),
        "invalid": sum(s["decision"] == "invalid" for s in steps),
    }
    return {
        "diagnosis": diagnosis,
        "severity": severity,
        "thresholds": THRESHOLDS.get(severity),
        "overall_risk": {"label": label, "max_step_risk": max_risk},
        "counts": counts,
        "steps": steps,
    }


# ---------------------------------------------------------------- sandbox

def _compact_source(source: str) -> str:
    """Same code without comments and docstrings, so the model copies less text into the sandbox."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def build_sandbox_script(incident: dict) -> tuple[str, dict]:
    """One script that tests every executable step against a staging clone.

    Returns (script, record). The record (nonce + snapshot) stays on the
    server so the reported output can be replayed and checked.
    """
    state = incidents.cluster()
    steps = [
        {"step_id": s["step_id"], "action_type": s["action_type"], "service": s["service"], "params": s["params"]}
        for s in incident["plan"]["steps"]
        if s["decision"] in ("auto_execute", "needs_approval")
    ]
    nonce = secrets.token_hex(8)
    source = _compact_source(Path(toy_cluster.__file__).read_text())
    script = f'''# Faah.ai sandbox test for {incident["id"]} (generated; runs a STAGING CLONE, never production)
{source}

STAGING = json.loads({json.dumps(json.dumps(state))})
STEPS = json.loads({json.dumps(json.dumps(steps))})
results = {{}}
for step in STEPS:
    results[step["step_id"]] = run_test(STAGING, step["action_type"], step["service"], step["params"])
projected = STAGING
for step in STEPS:
    if results[step["step_id"]]["passed"]:
        projected, _ = apply_action(projected, step["action_type"], step["service"], step["params"])
services = sorted({{s["service"] for s in STEPS}})
print("{SANDBOX_MARKER}" + dumps({{"nonce": "{nonce}", "results": results,
      "projected": {{name: metrics(projected, name) for name in services}}}}))
'''
    record = {"nonce": nonce, "snapshot": state, "steps": steps, "status": "script_issued", "result": None}
    return script, record


def verify_sandbox_output(record: dict, output: str) -> dict:
    """Parse the sandbox output and replay it on the server; they must agree exactly."""
    line = next((l for l in output.splitlines() if l.startswith(SANDBOX_MARKER)), None)
    if line is None:
        raise ValueError(f"no line starting with {SANDBOX_MARKER.strip()!r} in the sandbox output")
    reported = json.loads(line[len(SANDBOX_MARKER):])
    if reported.get("nonce") != record["nonce"]:
        raise ValueError("nonce mismatch: this output is not from the script issued for this plan")

    expected = {
        s["step_id"]: toy_cluster.run_test(record["snapshot"], s["action_type"], s["service"], s["params"])
        for s in record["steps"]
    }
    mismatched = [
        sid for sid, res in expected.items()
        if toy_cluster.dumps(res) != toy_cluster.dumps(reported["results"].get(sid))
    ]
    if mismatched:
        raise ValueError(f"sandbox output does not match the server replay for steps {mismatched}")
    return reported


# ---------------------------------------------------------------- verification

def verify(incident: dict, state: dict) -> dict:
    book = runbooks().get(incident.get("runbook_id") or "", {})
    targets = book.get("verify", {})
    service = (incident.get("parsed") or {}).get("service")
    if service not in state:
        return {"resolved": False, "checks": [], "reason": f"no metrics for {service!r}"}

    names = [service] + [n for n, s in state.items() if s.get("depends_on") == service]
    checks = []
    for name in names:
        m = toy_cluster.metrics(state, name)
        for key, limit in targets.items():
            metric = key.removesuffix("_below")
            if metric in m:
                checks.append({"service": name, "metric": metric, "value": m[metric], "limit": limit,
                               "ok": m[metric] < limit})
        checks.append({"service": name, "metric": "data_intact", "value": m.get("data_intact", True),
                       "limit": True, "ok": m.get("data_intact", True)})
    return {"resolved": all(c["ok"] for c in checks) and bool(targets), "checks": checks}


def to_markdown(incident: dict) -> str:
    plan = incident.get("plan") or {}
    sev = (incident.get("severity") or {}).get("severity", "?").upper()
    match = incident.get("runbook_match") or {}
    lines = [
        f"# {incident['id']} — {incident['email']['subject']}",
        "",
        f"**Severity:** {sev}  ",
        f"**Runbook:** {incident.get('runbook_id')} ({match.get('method')}, {match.get('confidence')})  ",
        f"**Diagnosis:** {plan.get('diagnosis', '—')}  ",
        f"**Overall plan risk:** {plan.get('overall_risk', {}).get('label', '?').upper()} "
        f"(max step risk {plan.get('overall_risk', {}).get('max_step_risk', 0):.2f})",
        "",
        "| # | Step | Floor | Jev risk | Reversible | Decision | Sandbox | Executed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in plan.get("steps", []):
        r = s.get("routing") or {}
        jev = r.get("jev") or {}
        floor = "BLOCK" if (r.get("floor") or {}).get("floor_triggered") else "pass"
        sandbox = (s.get("sandbox") or {}).get("passed")
        lines.append(
            f"| {s['step_id']} | {s['description']} | {floor} | {jev.get('risk_score', '—')} | "
            f"{jev.get('reversibility', '—')} ({jev.get('reversibility_confidence', '—')}) | {s['decision']} | "
            f"{'✓' if sandbox else '✗' if sandbox is False else '—'} | {(s.get('execution') or {}).get('status', '—')} |"
        )
    if incident.get("resolution"):
        lines += ["", f"**Resolution:** {incident['resolution'].get('summary')}"]
    return "\n".join(lines)
