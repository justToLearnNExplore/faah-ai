"""
Layer 2 — Jev calibrated risk classifier (TypeSafe AI System One).

For actions that pass the hardcoded floor, ask Jev for a fast, calibrated
read on reversibility, risk and route. Jev is also used for severity triage
and for scoring an LLM-chosen runbook match.

Every call FAILS CLOSED: if Jev is unreachable or errors, the answer is
"irreversible / max risk / hard_escalate / confidence 0", so the action
routes to a human instead of auto-executing.
"""

import os
import time

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient, TypeSafeError

JEV_MODEL = os.environ.get("JEV_MODEL") or None  # None -> account default

RISK_LEVELS = [
    "0 - No risk: read-only, or a trivially reversible change to a single stateless service.",
    "1 - Low: reversible change to one stateless service; worst case is brief, self-healing degradation.",
    "2 - Moderate: could degrade a customer-facing service for minutes, or touches shared config.",
    "3 - High: could cause an outage, or touches a stateful system, deploy state, auth or network policy.",
    "4 - Severe: likely data loss, security exposure, or a broad multi-service outage.",
]

_client: TypeSafeClient | None = None


def _get_client() -> TypeSafeClient:
    global _client
    if _client is None:
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise TypeSafeError("TYPESAFE_API_KEY is not set")
        # Reads TYPESAFE_API_KEY / TYPESAFE_BASE_URL from the environment. 30s rather than the
        # SDK's 10s default: plan steps are scored in parallel, and a timeout fails closed.
        _client = TypeSafeClient(timeout=30)
    return _client


def _fail_closed(error: Exception, started: float) -> dict:
    return {
        "reversibility": "unknown",
        "reversibility_confidence": 0.0,
        "risk_score": 1.0,
        "risk_confidence": 0.0,
        "route": "hard_escalate",
        "route_confidence": 0.0,
        "reason": f"Jev unavailable, failing closed: {error}",
        "error": str(error),
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def classify_action(incident_context: str, proposed_action: str, action_metadata: dict) -> dict:
    """
    Args:
        incident_context: short summary of what triggered this
            (e.g. "checkout-service 5xx spike, 40% error rate")
        proposed_action: the exact remediation step
            (e.g. "scale checkout-service replicas to 8")
        action_metadata: service, env, severity, tier, stateful, category, undo...

    Returns:
        dict with reversibility, risk_score (0..1), route and their confidences.
    """
    started = time.monotonic()
    state = {
        "incident": incident_context,
        "proposed_action": proposed_action,
        **{k: v for k, v in action_metadata.items() if v is not None},
    }
    try:
        response = _get_client().system_one(
            state=state,
            model=JEV_MODEL,
            questions={
                "reversibility": Choice(
                    instructions="Can the proposed action be fully undone quickly, with no data loss?",
                    criteria={
                        "reversible": "Fully undoable in minutes with a known undo (e.g. scaling, a config value with a recorded previous value).",
                        "irreversible": "Cannot be undone, or may lose data or state (deletes, drops, destructive shell, data directory changes, restarts that drop in-flight work).",
                        "unknown": "Not enough information to tell.",
                    },
                ),
                "risk": Score(
                    instructions="How risky is executing this action on production right now?",
                    criteria=RISK_LEVELS,
                ),
                "route": Choice(
                    instructions="How should an on-call automation system route this action?",
                    criteria={
                        "auto_execute": "Safe to run without a human: low risk, reversible, narrow blast radius.",
                        "human_review": "A human should review and approve before it runs.",
                        "hard_escalate": "Dangerous or suspicious; stop and escalate to a senior engineer.",
                    },
                ),
            },
        )
    except (TypeSafeError, Exception) as error:  # noqa: BLE001 - any failure must fail closed
        return _fail_closed(error, started)

    reversibility = response.choices["reversibility"]
    risk = response.scores["risk"]
    route = response.choices["route"]
    max_level = len(RISK_LEVELS) - 1
    risk_score = round(risk.score / max_level, 3)
    return {
        "reversibility": reversibility.choice,
        "reversibility_confidence": round(reversibility.confidence, 3),
        "risk_score": risk_score,
        "risk_level": round(risk.score, 2),
        "risk_confidence": round(risk.confidence, 3),
        "route": route.choice,
        "route_confidence": round(route.confidence, 3),
        "route_probabilities": {k: round(v, 3) for k, v in route.probabilities.items()},
        "reason": (
            f"Jev: {reversibility.choice} ({reversibility.confidence:.2f}), "
            f"risk {risk_score:.2f} (level {risk.score:.1f}/4), "
            f"route {route.choice} ({route.confidence:.2f})"
        ),
        "model": response.model,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def classify_severity(signals: dict) -> dict:
    """Jev's read on incident severity: low / high / critical, with confidence."""
    started = time.monotonic()
    try:
        response = _get_client().system_one(
            state=signals,
            model=JEV_MODEL,
            questions={
                "severity": Choice(
                    instructions="Classify the severity of this production incident.",
                    criteria={
                        "low": "Warning-level, a single internal or non-customer-facing service, error rate under 5%, no user impact.",
                        "high": "A customer-facing service is degraded: error rate 5-25%, p99 latency over 2x baseline, one region, or correlated with a recent deploy.",
                        "critical": "Outage or error rate over 25%; payments, auth or data path affected; multi-service or multi-region; data integrity or security signals.",
                    },
                ),
            },
        )
    except (TypeSafeError, Exception) as error:  # noqa: BLE001
        return {"severity": None, "confidence": 0.0, "error": str(error),
                "latency_ms": round((time.monotonic() - started) * 1000)}
    answer = response.choices["severity"]
    return {
        "severity": answer.choice,
        "confidence": round(answer.confidence, 3),
        "probabilities": {k: round(v, 3) for k, v in answer.probabilities.items()},
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def score_runbook_fit(alert: dict, runbook: dict, justification: str) -> dict:
    """Jev's probability that an LLM-selected runbook actually fits the alert."""
    started = time.monotonic()
    try:
        response = _get_client().system_one(
            state={"alert": alert, "runbook": runbook, "agent_justification": justification},
            model=JEV_MODEL,
            questions={
                "fits": Noul(
                    instructions="This runbook is the right playbook for this alert.",
                    criteria={"true": "The runbook's symptoms and remediation match the alert.",
                              "false": "The runbook is for a different kind of problem."},
                ),
            },
        )
    except (TypeSafeError, Exception) as error:  # noqa: BLE001
        return {"fit": 0.0, "error": str(error), "latency_ms": round((time.monotonic() - started) * 1000)}
    return {"fit": round(response.nouls["fits"].noul, 3), "latency_ms": round((time.monotonic() - started) * 1000)}
