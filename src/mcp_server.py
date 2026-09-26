"""
Faah.ai incident tools, served over MCP (streamable HTTP) to TrueForge.

TrueForge is the agent harness: it runs the model loop, calls these tools,
runs the sandbox (Daytona) and pauses for human approval on
`apply_gated_step`. This server holds the deterministic parts — parsing,
runbook matching, severity, the floor + Jev router, the toy target — and
enforces the safety rules server-side, so a confused or manipulated model
can't auto-apply a step that needs a human.

Run:  cd src && ../.venv/bin/python mcp_server.py
"""

from dotenv_min import load_env

load_env()  # before anything reads os.environ

import os  # noqa: E402
from typing import Any  # noqa: E402

import uvicorn
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import incidents
import plan as planner
import runbook_matcher
import severity as triage
import toy_cluster
from catalog import GENERIC_RUNBOOK_ID, runbook_summary, runbooks
from email_parser import parse_email
from jev_classifier import score_runbook_fit
import functools  # noqa: E402

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

TOKEN = os.environ.get("FAAH_MCP_TOKEN", "")
PORT = int(os.environ.get("FAAH_MCP_PORT", "8765"))
RUNBOOK_FIT_MIN = 0.5

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE_SAFE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE_GATED = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

mcp = MCPServer(
    name="faah-incident-tools",
    title="Faah.ai incident tools",
    instructions="Incident triage, runbook, investigation, planning, sandbox and remediation tools for Faah.ai.",
)


class FaahToolError(ToolError, ValueError):
    """A refusal or bad input the model should read (MCP hides the text of other exceptions)."""


def tool(annotations: ToolAnnotations):
    """Register an MCP tool whose ValueError/KeyError messages reach the model."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, KeyError) as exc:
                if isinstance(exc, FaahToolError):
                    raise
                raise FaahToolError(str(exc).strip("'\"")) from exc
        mcp.tool(annotations=annotations)(wrapper)
        return wrapper
    return decorator


def _status(incident_id: str, status: str, event: str, data: dict) -> dict:
    incident = incidents.update(incident_id, status=status)
    incidents.audit(incident_id, event, data)
    return incident


# ------------------------------------------------------------------ 1. triage

@tool(READ)
def parse_alert_email(incident_id: str) -> dict:
    """Step 1. Parse the alert email for this incident into structured signals
    (source, service, alert name, metric, value, threshold, priority, severity hints)."""
    incident = incidents.get(incident_id)
    email = incident["email"]
    parsed = parse_email(email["sender"], email["subject"], email["body"])
    incidents.update(incident_id, parsed=parsed)
    _status(incident_id, "parsed", "email.parsed", parsed)
    return {"incident_id": incident_id, "parsed": parsed, "raw_body": email["body"],
            "next": "match_runbook"}


@tool(READ)
def match_runbook(incident_id: str) -> dict:
    """Step 2. Map the parsed alert to a runbook: exact alert-name match, then
    keyword patterns. If `needs_llm_selection` is true, pick one from `catalog`
    with select_runbook (or choose generic-triage if nothing fits)."""
    incident = incidents.get(incident_id)
    if not incident.get("parsed"):
        raise ValueError("call parse_alert_email first")
    result = runbook_matcher.match(incident["parsed"])
    fields = {"runbook_match": result}
    if result["runbook_id"]:
        fields["runbook_id"] = result["runbook_id"]
        fields["status"] = "runbook_matched"
    incidents.update(incident_id, **fields)
    incidents.audit(incident_id, "runbook.match", result)
    if result["runbook_id"]:
        result["runbook"] = runbooks()[result["runbook_id"]]
        result["next"] = "assess_severity"
    else:
        result["next"] = "select_runbook: pick the best runbook id from `catalog` (or generic-triage) with a one-sentence justification"
    return result


def result_catalog() -> list[dict]:
    return [runbook_summary(b) for b in runbooks().values() if b["id"] != GENERIC_RUNBOOK_ID]


@tool(READ)
def select_runbook(incident_id: str, runbook_id: str, justification: str) -> dict:
    """Step 2 (fallback). Choose a runbook yourself when match_runbook could not.
    Jev scores how well it fits; below 0.5 the incident falls back to
    generic-triage (read-only) and is flagged for a human."""
    incident = incidents.get(incident_id)
    books = runbooks()
    if runbook_id not in books:
        raise ValueError(f"unknown runbook {runbook_id!r}; choose from {sorted(books)}")

    if runbook_id == GENERIC_RUNBOOK_ID:
        fit = {"fit": None}
        chosen, flagged = GENERIC_RUNBOOK_ID, True
    else:
        fit = score_runbook_fit(incident["parsed"], runbook_summary(books[runbook_id]), justification)
        flagged = fit["fit"] < RUNBOOK_FIT_MIN
        chosen = GENERIC_RUNBOOK_ID if flagged else runbook_id

    result = {
        "runbook_id": chosen,
        "method": "llm",
        "llm_choice": runbook_id,
        "justification": justification,
        "confidence": fit["fit"],
        "jev": fit,
        "flagged_for_human": flagged,
        "needs_llm_selection": False,
    }
    incidents.update(incident_id, runbook_match=result, runbook_id=chosen, status="runbook_matched")
    incidents.audit(incident_id, "runbook.selected", result)
    if flagged:
        options = [b["id"] for b in result_catalog()][:4]
        result["next"] = ("ask_user_question: ask the on-call human which runbook applies, options "
                          f"{options + ['Investigate only (escalate)']}; then select_runbook with their answer")
    else:
        result["next"] = "assess_severity"
    return {**result, "runbook": books[chosen]}


@tool(READ)
def assess_severity(incident_id: str) -> dict:
    """Step 3. Grade the incident LOW / HIGH / CRITICAL from the email rules,
    live metrics and Jev (the highest wins). Call after the runbook is chosen."""
    incident = incidents.get(incident_id)
    parsed = incident.get("parsed") or {}
    state = incidents.cluster()
    service = parsed.get("service")
    metrics = None
    if service in state:
        metrics = toy_cluster.metrics(state, service)
        metrics["dependants"] = {
            n: toy_cluster.metrics(state, n) for n, s in state.items() if s.get("depends_on") == service
        }
    book = runbooks().get(incident.get("runbook_id") or "")
    result = triage.assess(parsed, book, metrics)
    incidents.update(incident_id, severity=result)
    _status(incident_id, "severity_assigned", "severity.assigned", result)
    tools = next((st["tools"] for st in (book or {}).get("steps", []) if st["kind"] == "investigate"), ["get_metrics", "get_logs"])
    return {**result, "next": f"investigate {service} with {tools}, then submit_remediation_plan with every step"}


# ------------------------------------------------------------------ 2. investigation (read-only)

def _investigate(incident_id: str, tool: str, service: str, result: Any) -> Any:
    incident = incidents.get(incident_id)
    incident["investigation"].append({"tool": tool, "service": service, "at": incidents.now(), "result": result})
    if incident["status"] in ("parsed", "runbook_matched", "severity_assigned"):
        incident["status"] = "investigating"
    incidents.save(incident)
    incidents.audit(incident_id, f"investigate.{tool}", {"service": service})
    return result


def _service_state(service: str) -> dict:
    state = incidents.cluster()
    if service not in state:
        raise ValueError(f"unknown service {service!r}; known: {sorted(state)}")
    return state


@tool(READ)
def get_metrics(incident_id: str, service: str) -> dict:
    """Read-only. Current golden-signal metrics for a service (and its dependants)."""
    state = _service_state(service)
    result = toy_cluster.metrics(state, service)
    result["dependants"] = {n: toy_cluster.metrics(state, n) for n, s in state.items() if s.get("depends_on") == service}
    return _investigate(incident_id, "get_metrics", service, result)


@tool(READ)
def get_logs(incident_id: str, service: str) -> dict:
    """Read-only. Recent log lines for a service."""
    return _investigate(incident_id, "get_logs", service, {"service": service, "lines": toy_cluster.logs(_service_state(service), service)})


@tool(READ)
def get_deploy_history(incident_id: str, service: str) -> dict:
    """Read-only. Deploy history for a service (oldest first) and the running version."""
    svc = _service_state(service)[service]
    return _investigate(incident_id, "get_deploy_history", service,
                        {"service": service, "running": svc.get("version"), "history": svc.get("deploy_history", [])})


@tool(READ)
def list_pods(incident_id: str, service: str) -> dict:
    """Read-only. Pods and restart counts for a service."""
    return _investigate(incident_id, "list_pods", service, {"service": service, "pods": toy_cluster.pods(_service_state(service), service)})


@tool(READ)
def describe_service(incident_id: str, service: str) -> dict:
    """Read-only. Configuration of a service (replicas, limits, version, disk, connections)."""
    svc = dict(_service_state(service)[service])
    svc.pop("deploy_history", None)
    return _investigate(incident_id, "describe_service", service, {"service": service, **svc})


# ------------------------------------------------------------------ 3. plan

class PlanStep(BaseModel):
    action_type: str = Field(description="One of the runbook's remediation action types, e.g. scale, set_memory_limit, rollback")
    service: str | None = Field(default=None, description="Target service (defaults to the alerting service)")
    params: dict[str, Any] = Field(default_factory=dict, description="Action parameters, e.g. {'replicas': 8}")
    title: str | None = Field(default=None, description="Short human title for the step")
    description: str | None = Field(default=None, description="What the step does, in your words")
    rationale: str = Field(description="Why this step is in the plan")
    evidence: str = Field(description="Which logs/metrics support it")
    expected_outcome: str = Field(description="What should change if it works, and how to verify")


@tool(READ)
def submit_remediation_plan(incident_id: str, diagnosis: str, steps: list[PlanStep]) -> dict:
    """Step 5. Submit the COMPLETE remediation plan (every step, in order) before
    changing anything. Each step is scored: hardcoded floor, Jev risk analysis,
    severity thresholds, blast radius and undo. Returns each step's decision
    (auto_execute / needs_approval / invalid). Resubmitting replaces the plan."""
    incident = incidents.get(incident_id)
    if not incident.get("severity"):
        raise ValueError("call assess_severity before submitting a plan")
    if incident.get("runbook_id") == GENERIC_RUNBOOK_ID and steps:
        raise ValueError("generic-triage is read-only: ask the on-call human which runbook to use before proposing remediation")
    new_plan = planner.build_plan(incident, diagnosis, [s.model_dump() for s in steps])
    new_plan["submitted_at"] = incidents.now()
    incidents.update(incident_id, plan=new_plan, sandbox=None)
    _status(incident_id, "plan_ready", "plan.submitted", {
        "diagnosis": diagnosis, "overall_risk": new_plan["overall_risk"], "counts": new_plan["counts"],
        "steps": [{k: s[k] for k in ("step_id", "description", "decision")} | {"reasons": s["routing"]["reasons"]}
                  for s in new_plan["steps"]],
    })
    executable = [st for st in new_plan["steps"] if st["decision"] in ("auto_execute", "needs_approval")]
    if new_plan["counts"]["invalid"]:
        new_plan["next"] = "fix the invalid steps (see each step's routing.reasons) and call submit_remediation_plan again"
    elif executable:
        new_plan["next"] = "get_sandbox_test_script, then run its exec_command with the sandbox exec tool"
    else:
        new_plan["next"] = "verify_incident, then resolve_incident"
    return new_plan


def _apply_plan_next(incident: dict) -> str:
    """Name the exact tool for the next unexecuted step, in plan order."""
    for st in incident["plan"]["steps"]:
        if st["decision"] not in ("auto_execute", "needs_approval") or st.get("execution"):
            continue
        tool_name = "apply_safe_step" if st["decision"] == "auto_execute" else "apply_gated_step"
        return f'{tool_name}(step_id="{st["step_id"]}")  # {st["description"]}'
    return "verify_incident, then resolve_incident"


# ------------------------------------------------------------------ 4. sandbox

@tool(READ)
def get_sandbox_test_script(incident_id: str) -> dict:
    """Step 6a. Get a self-contained Python script that tests every executable
    plan step against a STAGING CLONE of production. Write it to a file in
    your sandbox (e.g. faah_sandbox_test.py), run it with python3, and pass
    the full stdout to record_sandbox_results."""
    incident = incidents.get(incident_id)
    if not incident.get("plan"):
        raise ValueError("submit a plan first")
    script, record = planner.build_sandbox_script(incident)
    incidents.update(incident_id, sandbox=record)
    incidents.audit(incident_id, "sandbox.script_issued", {"steps": [s["step_id"] for s in record["steps"]]})
    command = f"cat > faah_sandbox_test.py <<'FAAH_EOF'\n{script}\nFAAH_EOF\npython3 faah_sandbox_test.py"
    return {
        "exec_command": command,
        "next": ("call the sandbox exec tool ONCE with command set to exec_command exactly as given "
                 "(do not pass cwd, do not edit it), then pass the full stdout to record_sandbox_results. "
                 "If exec fails twice, stop and resolve_incident with outcome escalated."),
    }


@tool(READ)
def record_sandbox_results(incident_id: str, sandbox_output: str) -> dict:
    """Step 6b. Record what the sandbox printed. The server replays the same
    test and rejects output that doesn't match. A step can only be applied
    after it passed here."""
    incident = incidents.get(incident_id)
    record = incident.get("sandbox")
    if not record:
        raise ValueError("call get_sandbox_test_script first")
    reported = planner.verify_sandbox_output(record, sandbox_output)

    for step in incident["plan"]["steps"]:
        res = reported["results"].get(step["step_id"])
        if res is None:
            continue
        step["sandbox"] = {"passed": res["passed"], "reasons": res["reasons"], "before": res["before"],
                           "after": res["after"], "dependants_after": res["dependants_after"],
                           "data_loss": res["result"].get("data_loss", False)}
        if not res["passed"] and step["decision"] == "auto_execute":
            step["decision"] = "needs_approval"
            step["routing"]["reasons"].append("Sandbox test failed: " + "; ".join(res["reasons"]))
    record.update(status="verified", result=reported, verified_at=incidents.now())
    incident["sandbox"] = record
    incident["plan"]["projected"] = reported["projected"]
    incident["status"] = "sandbox_tested"
    incidents.save(incident)
    summary = {s["step_id"]: (s.get("sandbox") or {}).get("passed") for s in incident["plan"]["steps"]}
    incidents.audit(incident_id, "sandbox.verified", {"results": summary})
    return {"results": summary, "projected_after_passing_steps": reported["projected"],
            "steps": [{k: s[k] for k in ("step_id", "description", "decision")} for s in incident["plan"]["steps"]],
            "next": _apply_plan_next(incident)}


# ------------------------------------------------------------------ 5. execution

def _apply(incident: dict, step: dict, mode: str, approved_by: str | None) -> dict:
    state = incidents.cluster()
    new_state, result = toy_cluster.apply_action(state, step["action_type"], step["service"], step["params"])
    incidents.save_cluster(new_state)
    step["execution"] = {
        "status": "applied", "mode": mode, "approved_by": approved_by, "at": incidents.now(),
        "result": result, "metrics_after": toy_cluster.metrics(new_state, step["service"]),
    }
    incident["status"] = "executing"
    incident["executions"].append({"step_id": step["step_id"], **step["execution"]})
    incidents.save(incident)
    incidents.audit(incident["id"], f"step.applied.{mode}", {"step_id": step["step_id"], "description": step["description"],
                                                             "approved_by": approved_by, "result": result})
    return {"step_id": step["step_id"], "applied": True, "mode": mode, **step["execution"],
            "next": _apply_plan_next(incident)}


def _executable(incident: dict, step_id: str) -> dict:
    step = incidents.find_step(incident, step_id)
    if step["decision"] in ("invalid", "read_only"):
        raise ValueError(f"{step_id} is {step['decision']} and cannot be executed")
    if (step.get("execution") or {}).get("status") == "applied":
        raise ValueError(f"{step_id} was already applied")
    sandbox = step.get("sandbox")
    if not sandbox:
        raise ValueError(f"{step_id} has no sandbox result. Run get_sandbox_test_script, execute its exec_command "
                         "in the sandbox and call record_sandbox_results before applying any step")
    if sandbox.get("data_loss"):
        raise ValueError(f"{step_id} caused DATA LOSS in the sandbox; it cannot be executed, even with approval")
    return step


@tool(WRITE_SAFE)
def apply_safe_step(incident_id: str, step_id: str) -> dict:
    """Step 7a. Apply a step whose decision is auto_execute and whose sandbox
    test passed. Runs without a human. Refused for anything else — use
    apply_gated_step for steps that need approval."""
    incident = incidents.get(incident_id)
    step = _executable(incident, step_id)
    sev = (incident.get("severity") or {}).get("severity")
    problems = []
    if step["decision"] != "auto_execute":
        problems.append(f"decision is {step['decision']}")
    if sev == "critical":
        problems.append("incident is CRITICAL")
    if not step["sandbox"]["passed"]:
        problems.append("sandbox test did not pass")
    if problems:
        incidents.audit(incident_id, "step.auto_refused", {"step_id": step_id, "problems": problems})
        raise ValueError(f"{step_id} is not safe to auto-apply ({'; '.join(problems)}). Use apply_gated_step to request human approval.")
    return _apply(incident, step, "auto", None)


@tool(WRITE_GATED)
def apply_gated_step(incident_id: str, step_id: str) -> dict:
    """Step 7b. Apply a step that needs human approval. TrueForge pauses this call
    for the on-call human; it only runs after they approve on the dashboard.
    Call it for ONE step at a time."""
    incident = incidents.get(incident_id)
    step = _executable(incident, step_id)
    approval = incident.get("approvals", {}).get(step_id)
    # Defence in depth: TrueForge pauses this tool for approval, and the
    # orchestrator also records the human decision here before resuming.
    if not approval or approval.get("decision") != "allow":
        incidents.audit(incident_id, "step.gated_refused", {"step_id": step_id, "approval": approval})
        raise ValueError(f"{step_id} has no recorded human approval")
    return _apply(incident, step, "approved", approval.get("by"))


# ------------------------------------------------------------------ 6. verify + close

@tool(READ)
def verify_incident(incident_id: str) -> dict:
    """Step 8. Check the service (and dependants) against the runbook's verify thresholds."""
    incident = incidents.get(incident_id)
    result = planner.verify(incident, incidents.cluster())
    result["at"] = incidents.now()
    incidents.update(incident_id, verification=result)
    incidents.audit(incident_id, "incident.verified", result)
    outcome = "resolved" if result["resolved"] else "partially_resolved or escalated"
    return {**result, "next": f"resolve_incident with outcome {outcome}"}


@tool(READ)
def resolve_incident(incident_id: str, summary: str, outcome: str) -> dict:
    """Step 9. Close out: outcome is 'resolved' (verify passed), 'partially_resolved'
    (some steps rejected or still failing) or 'escalated' (handed to humans)."""
    if outcome not in ("resolved", "partially_resolved", "escalated"):
        raise ValueError("outcome must be resolved, partially_resolved or escalated")
    incident = incidents.get(incident_id)
    if outcome == "resolved" and not (incident.get("verification") or {}).get("resolved"):
        raise ValueError("verify_incident has not passed; use partially_resolved or escalated")
    resolution = {"outcome": outcome, "summary": summary, "at": incidents.now()}
    incidents.update(incident_id, resolution=resolution)
    _status(incident_id, outcome, "incident.closed", resolution)
    return resolution


@tool(READ)
def get_incident(incident_id: str) -> dict:
    """Current state of the incident: parsed alert, runbook, severity, plan, sandbox and executions."""
    incident = incidents.get(incident_id)
    incident.pop("sandbox", None)
    return incident


# ------------------------------------------------------------------ internal HTTP API (orchestrator only)

@mcp.custom_route("/internal/incidents", methods=["POST"])
async def create_incident(request: Request):
    body = await request.json()
    incident = incidents.create(body.get("sender", ""), body.get("subject", ""), body.get("body", ""),
                                body.get("received_via", "unknown"))
    return JSONResponse(incident, status_code=201)


@mcp.custom_route("/internal/incidents", methods=["GET"])
async def list_incidents(request: Request):
    return JSONResponse(incidents.list_all())


@mcp.custom_route("/internal/incidents/{incident_id}", methods=["GET"])
async def read_incident(request: Request):
    try:
        incident = incidents.get(request.path_params["incident_id"])
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if incident.get("sandbox"):
        incident["sandbox"] = {k: v for k, v in incident["sandbox"].items() if k != "snapshot"}
    incident["audit"] = incidents.audit_log(incident["id"])
    return JSONResponse(incident)


@mcp.custom_route("/internal/incidents/{incident_id}/markdown", methods=["GET"])
async def incident_markdown(request: Request):
    return PlainTextResponse(planner.to_markdown(incidents.get(request.path_params["incident_id"])))


@mcp.custom_route("/internal/incidents/{incident_id}/approvals", methods=["POST"])
async def record_approval(request: Request):
    incident_id = request.path_params["incident_id"]
    body = await request.json()
    incident = incidents.get(incident_id)
    incidents.find_step(incident, body["step_id"])
    decision = {"decision": body["decision"], "by": body.get("by", "on-call"), "reason": body.get("reason"),
                "at": incidents.now()}
    incident.setdefault("approvals", {})[body["step_id"]] = decision
    if body["decision"] != "allow":
        step = incidents.find_step(incident, body["step_id"])
        step["execution"] = {"status": "rejected", "by": decision["by"], "reason": decision["reason"], "at": decision["at"]}
    incidents.save(incident)
    incidents.audit(incident_id, f"human.{body['decision']}", {"step_id": body["step_id"], **decision})
    return JSONResponse(decision)


@mcp.custom_route("/internal/incidents/{incident_id}/events", methods=["POST"])
async def record_event(request: Request):
    """Orchestrator-side events (session started, approval requested, turn errors) for the audit trail."""
    incident_id = request.path_params["incident_id"]
    body = await request.json()
    if body.get("status"):
        incidents.update(incident_id, status=body["status"])
    incidents.audit(incident_id, body["event"], body.get("data"))
    return JSONResponse({"ok": True})


@mcp.custom_route("/internal/cluster", methods=["GET"])
async def read_cluster(request: Request):
    state = incidents.cluster()
    return JSONResponse({name: toy_cluster.metrics(state, name) for name in state})


@mcp.custom_route("/internal/cluster/reset", methods=["POST"])
async def reset_cluster(request: Request):
    incidents.reset_cluster()
    return JSONResponse({"ok": True})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"ok": True})


class TokenGuard:
    """Require X-Faah-Token on /mcp and /internal (TrueForge sends it via header auth)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = dict(scope.get("headers") or [])
            if not TOKEN or headers.get(b"x-faah-token", b"").decode() != TOKEN:
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    if not TOKEN:
        raise SystemExit("FAAH_MCP_TOKEN is not set (see .env.example)")
    incidents.cluster()  # create the toy cluster on first run
    app = TokenGuard(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
