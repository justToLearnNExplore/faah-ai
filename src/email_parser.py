"""
Pattern identification, part 1: turn a raw alert email into structured signals.

Handles the common alerting formats (Datadog, Grafana, PagerDuty, CloudWatch,
Prometheus Alertmanager) with regexes over sender, subject and body. Anything
not found is left as None; the runbook matcher and severity triage cope with
missing fields.
"""

import re

from catalog import services

SOURCES = {
    "datadog": ["datadoghq.com", "datadog"],
    "grafana": ["grafana.net", "grafana"],
    "pagerduty": ["pagerduty.com", "pagerduty"],
    "cloudwatch": ["amazonaws.com", "cloudwatch", "aws"],
    "alertmanager": ["alertmanager", "prometheus"],
}

SEVERITY_WORDS = {
    "critical": [r"\bcritical\b", r"\bP1\b", r"\bSEV-?1\b", r"\boutage\b", r"\bdown\b"],
    "high": [r"\bhigh\b", r"\bP2\b", r"\bSEV-?2\b", r"\bdegraded\b", r"\berror\b"],
    "low": [r"\bwarn(ing)?\b", r"\blow\b", r"\bP[34]\b", r"\bSEV-?[34]\b", r"\binfo\b"],
}

FIELD_PATTERNS = {
    "alert_name": [r"alert(?:[ _]?name)?\s*[:=]\s*([A-Za-z0-9_.-]+)", r"alarm\s*[:=]\s*([A-Za-z0-9_.-]+)"],
    "service": [r"service\s*[:=]\s*([a-z0-9][a-z0-9-]+)", r"\bservice/([a-z0-9-]+)"],
    "env": [r"\benv(?:ironment)?\s*[:=]\s*([a-z0-9-]+)"],
    "region": [r"\bregion\s*[:=]\s*([a-z0-9-]+)"],
    "metric": [r"\bmetric\s*[:=]\s*([A-Za-z0-9_.]+)"],
    "value": [r"\bvalue\s*[:=]\s*([0-9.]+%?)", r"\bcurrent\s*[:=]\s*([0-9.]+%?)"],
    "threshold": [r"\bthreshold\s*[:=]\s*([<>]?\s*[0-9.]+%?)"],
    "priority": [r"\bpriority\s*[:=]\s*(P[0-9]|SEV-?[0-9]|critical|high|low|warning)"],
}


def _first(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _to_number(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"[0-9.]+", raw)
    if not m:
        return None
    n = float(m.group(0))
    return n / 100 if raw.strip().endswith("%") else n


def parse_email(sender: str, subject: str, body: str) -> dict:
    sender = sender or ""
    subject = subject or ""
    body = body or ""
    # Body first: structured "key: value" fields live there; subjects often start with "ALARM:" etc.
    text = f"{body}\n{subject}"

    source = "unknown"
    lowered_sender = sender.lower()
    for name, needles in SOURCES.items():
        if any(n in lowered_sender for n in needles):
            source = name
            break

    fields = {key: _first(pats, text) for key, pats in FIELD_PATTERNS.items()}

    # Fall back to any known service name mentioned anywhere in the email.
    if not fields["service"]:
        for name in services():
            if name in text:
                fields["service"] = name
                break

    severity_hints = [
        level for level, pats in SEVERITY_WORDS.items()
        if any(re.search(p, subject, re.IGNORECASE) for p in pats)
        or (fields["priority"] and any(re.search(p, fields["priority"], re.IGNORECASE) for p in pats))
    ]

    return {
        "source": source,
        "sender": sender,
        "subject": subject,
        "alert_name": fields["alert_name"],
        "service": fields["service"],
        "env": fields["env"] or "production",
        "region": fields["region"],
        "metric": fields["metric"],
        "value": fields["value"],
        "value_numeric": _to_number(fields["value"]),
        "threshold": fields["threshold"],
        "priority": fields["priority"],
        "severity_hints": severity_hints,
        "body_excerpt": body[:1500],
    }
