"""
Layer 1 — Hardcoded safety floor.

These rules fire regardless of what Jev (or any model) says. This is the
non-negotiable safety net: even if the calibrated classifier is wrong or
tricked, actions matching these patterns always route to human approval.
"""

import re

ALWAYS_ESCALATE_PATTERNS = [
    # Data destruction
    r"DROP\s+(TABLE|DATABASE|SCHEMA)",
    r"DELETE\s+FROM",
    r"TRUNCATE",
    r"rm\s+-[a-z]*(rf|fr)",
    r"rm\s+(-[a-z]+\s+)*/",
    r"\.delete\(",
    r"pg_wal|pg_xlog|/var/lib/(postgresql|mysql)",
    r"mkfs|dd\s+if=|shred",

    # Service/infra shutdown
    r"scale.*to\s+0\b",
    r"scale.*to.*zero",
    r"replicas?\s*[:=]?\s*(to\s+)?0\b",
    r"terminate.*instance",
    r"delete.*deployment",
    r"kubectl\s+delete",

    # Stateful service restarts (data-loss risk)
    r"restart.*primary",
    r"restart.*database",
    r"restart.*master",
    r"failover",

    # Deploy/rollback actions
    r"rollback",
    r"revert.*deploy",
    r"force.*push",

    # Access/security changes
    r"revoke.*access",
    r"delete.*user",
    r"drop.*permission",
    r"disable.*auth",
]

# Action types that always need a human, whatever their description says.
ALWAYS_ESCALATE_ACTION_TYPES = {
    "rollback": "deploy state change",
    "restart_primary": "stateful primary restart",
    "set_max_connections": "requires a database primary restart",
    "run_shell": "arbitrary shell command",
}

# Pure reads — never even need Jev, never need approval
ALWAYS_SAFE_ACTIONS = [
    "get_logs",
    "get_metrics",
    "get_deploy_history",
    "list_pods",
    "describe_service",
    "check_status",
]


def check_hardcoded_floor(action_description: str, action_type: str, command: str | None = None) -> dict:
    """Returns the floor verdict BEFORE Jev is ever called.

    Args:
        action_description: free-text description of the proposed action
            (e.g. "scale checkout-service replicas to 8")
        action_type: a short tag for the action (e.g. "get_logs", "scale",
            "rollback")
        command: the literal command, if the step carries one (run_shell)

    Returns:
        {"floor_triggered": bool, "reason": str | None, "matched": [str]}
    """
    if action_type in ALWAYS_SAFE_ACTIONS:
        return {"floor_triggered": False, "reason": None, "matched": []}

    # Collapse whitespace so "rm   -rf" or a line break can't slip past a pattern.
    text = " ".join(f"{action_description or ''} {command or ''}".split())

    matched = []
    if action_type in ALWAYS_ESCALATE_ACTION_TYPES:
        matched.append(f"action_type={action_type} ({ALWAYS_ESCALATE_ACTION_TYPES[action_type]})")
    for pattern in ALWAYS_ESCALATE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            matched.append(pattern)

    if matched:
        return {
            "floor_triggered": True,
            "reason": "Matched hardcoded rule: " + "; ".join(matched),
            "matched": matched,
        }
    return {"floor_triggered": False, "reason": None, "matched": []}
