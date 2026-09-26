"""
Combined routing logic: hardcoded floor first, then Jev's calibrated score,
then severity-aware thresholds and the rest of the "safe" definition.

This is called for every remediation step in a plan. It returns a decision
plus every reason, for both the approval-gate UI and the audit trail.
"""

from hardcoded_floor import ALWAYS_SAFE_ACTIONS, check_hardcoded_floor
from jev_classifier import classify_action

# Severity-aware thresholds -- show these on screen during the demo.
# Auto-execute needs Jev risk BELOW max_risk and route confidence ABOVE min_confidence.
THRESHOLDS = {
    "low": {"max_risk": 0.30, "min_confidence": 0.80},
    "high": {"max_risk": 0.20, "min_confidence": 0.85},
    "critical": None,  # never auto-execute
}
REVERSIBILITY_MIN_CONFIDENCE = 0.85

# Categories whose blast radius is never "small" (see "What Safe Means").
UNSAFE_CATEGORIES = {"database", "deploy", "shell", "auth", "network", "data"}


def route_action(
    incident_context: str,
    proposed_action: str,
    action_type: str,
    action_metadata: dict,
    severity: str = "high",
    command: str | None = None,
) -> dict:
    """
    Returns:
        {
          "decision": "read_only" | "auto_execute" | "needs_approval",
          "source": "read_only" | "hardcoded_floor" | "jev",
          "reasons": [why approval is needed],   # empty for auto_execute
          "floor": {...}, "jev": {...} | None, "thresholds": {...} | None,
        }
    """
    if action_type in ALWAYS_SAFE_ACTIONS:
        return {"decision": "read_only", "source": "read_only", "reasons": [],
                "floor": {"floor_triggered": False}, "jev": None, "thresholds": None}

    severity = severity if severity in THRESHOLDS else "high"
    thresholds = THRESHOLDS[severity]
    reasons = []

    # Layer 1: hardcoded floor. Jev still runs so the plan shows a risk analysis
    # for every step, but nothing Jev says can override the floor.
    floor = check_hardcoded_floor(proposed_action, action_type, command)
    if floor["floor_triggered"]:
        reasons.append(floor["reason"])

    # Layer 2: Jev calibrated classifier.
    jev = classify_action(incident_context, proposed_action, {**action_metadata, "severity": severity})
    if jev.get("error"):
        reasons.append(jev["reason"])

    if thresholds is None:
        reasons.append("Incident severity is CRITICAL: every remediation step needs human approval")
    else:
        if jev["risk_score"] >= thresholds["max_risk"]:
            reasons.append(f"Jev risk {jev['risk_score']:.2f} ≥ {thresholds['max_risk']:.2f} ({severity} threshold)")
        if jev["route_confidence"] <= thresholds["min_confidence"]:
            reasons.append(f"Jev confidence {jev['route_confidence']:.2f} ≤ {thresholds['min_confidence']:.2f} ({severity} threshold)")
    if jev["route"] != "auto_execute":
        reasons.append(f"Jev route = {jev['route']}")
    if jev["reversibility"] != "reversible" or jev["reversibility_confidence"] < REVERSIBILITY_MIN_CONFIDENCE:
        reasons.append(
            f"Jev reversibility = {jev['reversibility']} ({jev['reversibility_confidence']:.2f}); "
            f"needs reversible with ≥ {REVERSIBILITY_MIN_CONFIDENCE:.2f}"
        )

    # Rest of the "safe" definition: small blast radius and an explicit undo.
    if action_metadata.get("stateful"):
        reasons.append(f"Blast radius: {action_metadata.get('service')} is stateful")
    if action_metadata.get("category") in UNSAFE_CATEGORIES:
        reasons.append(f"Blast radius: {action_metadata.get('category')} action")
    if not action_metadata.get("undo"):
        reasons.append("No explicit undo action defined")

    return {
        "decision": "needs_approval" if reasons else "auto_execute",
        "source": "hardcoded_floor" if floor["floor_triggered"] else "jev",
        "reasons": reasons,
        "floor": floor,
        "jev": jev,
        "thresholds": thresholds,
        "severity": severity,
    }


if __name__ == "__main__":
    # Quick manual smoke test (needs TYPESAFE_API_KEY)
    example = route_action(
        incident_context="checkout-service 5xx spike, 40% error rate",
        proposed_action="scale checkout-service replicas to 8",
        action_type="scale",
        action_metadata={"service": "checkout-service", "env": "production", "stateful": False,
                         "category": "compute", "undo": {"action_type": "scale", "params": {"replicas": 3}}},
        severity="high",
    )
    print(example)
