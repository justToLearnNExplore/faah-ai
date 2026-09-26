"""
Incident severity triage: LOW / HIGH / CRITICAL (PLAN.md section 1).

Three passes, and the highest result wins:
  1. rules   - severity words in the email, priority field, service tier
  2. metrics - live metrics can only RAISE severity, never lower it
  3. Jev     - calibrated severity choice; if its confidence < 0.70, bump up one level
"""

from catalog import service_info
from jev_classifier import classify_severity

LEVELS = ["low", "high", "critical"]
JEV_MIN_CONFIDENCE = 0.70


def _max(*levels: str | None) -> str:
    return max((l for l in levels if l in LEVELS), key=LEVELS.index, default="high")


def _bump(level: str) -> str:
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]


def rules_pass(parsed: dict, runbook: dict | None) -> tuple[str, list[str]]:
    reasons = []
    level = runbook.get("default_severity", "low") if runbook else "low"
    if runbook:
        reasons.append(f"runbook {runbook['id']} default: {level}")

    for hint in parsed.get("severity_hints", []):
        if LEVELS.index(hint) > LEVELS.index(level):
            reasons.append(f"email says '{hint}' (subject/priority)")
        level = _max(level, hint)

    info = service_info(parsed.get("service"))
    if info["tier"] == 0 and level == "low":
        level = "high"
        reasons.append(f"{parsed.get('service')} is tier-0 (money path)")
    if not info.get("customer_facing") and level != "critical":
        reasons.append(f"{parsed.get('service')} is internal only")
    return level, reasons


def metrics_pass(metrics: dict | None, service: str | None) -> tuple[str, list[str]]:
    if not metrics:
        return "low", ["no live metrics available"]
    reasons = []
    level = "low"
    info = service_info(service)
    error = metrics.get("error_rate") or 0
    if not metrics.get("data_intact", True):
        return "critical", ["data integrity failure"]
    if error > 0.25:
        level = "critical"
        reasons.append(f"error rate {error:.0%} > 25%")
    elif error >= 0.05:
        level = "high"
        reasons.append(f"error rate {error:.0%} ≥ 5%")
    if (metrics.get("p99_ms") or 0) > 1600 and info.get("customer_facing"):
        level = _max(level, "high")
        reasons.append(f"p99 {metrics['p99_ms']}ms > 2x baseline")
    if (metrics.get("connection_util") or 0) > 0.95:
        level = _max(level, "critical" if info["tier"] == 0 else "high")
        reasons.append(f"connection pool {metrics['connection_util']:.0%} saturated")
    if (metrics.get("disk_used_pct") or 0) > 90 and info.get("stateful"):
        level = _max(level, "high")
        reasons.append(f"database disk {metrics['disk_used_pct']}% > 90%")
    for name, dep in (metrics.get("dependants") or {}).items():
        if (dep.get("error_rate") or 0) > 0.25:
            level = "critical"
            reasons.append(f"dependant {name} error rate {dep['error_rate']:.0%}")
    return level, reasons


def assess(parsed: dict, runbook: dict | None, metrics: dict | None) -> dict:
    rules_level, rules_reasons = rules_pass(parsed, runbook)
    metrics_level, metrics_reasons = metrics_pass(metrics, parsed.get("service"))

    signals = {
        "alert": {k: parsed.get(k) for k in ("source", "subject", "alert_name", "service", "env", "region",
                                              "metric", "value", "threshold", "priority")},
        "service_catalog": service_info(parsed.get("service")),
        "runbook": runbook["id"] if runbook else None,
        "live_metrics": metrics,
    }
    jev = classify_severity(signals)
    jev_level = jev.get("severity")
    jev_reasons = []
    if jev_level is None:
        jev_reasons.append(f"Jev unavailable ({jev.get('error')}); bumping one level")
    elif jev["confidence"] < JEV_MIN_CONFIDENCE:
        jev_reasons.append(f"Jev says {jev_level} with low confidence {jev['confidence']:.2f} < {JEV_MIN_CONFIDENCE}; bumping one level")
    else:
        jev_reasons.append(f"Jev says {jev_level} ({jev['confidence']:.2f})")

    level = _max(rules_level, metrics_level, jev_level)
    if jev_level is None or jev["confidence"] < JEV_MIN_CONFIDENCE:
        level = _bump(level)

    return {
        "severity": level,
        "passes": {
            "rules": {"level": rules_level, "reasons": rules_reasons},
            "metrics": {"level": metrics_level, "reasons": metrics_reasons},
            "jev": {"level": jev_level, "reasons": jev_reasons, **jev},
        },
        "reason": "; ".join(rules_reasons + metrics_reasons + jev_reasons),
    }
