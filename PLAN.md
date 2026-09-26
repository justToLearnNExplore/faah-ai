# Faah.ai — On-Call Runbook Executor Agent

**Hackathon:** Agents That Act — TrueFoundry × Polaris Hackathon
**Category:** Full Runbook Executor
**Stack:** TrueForge (agent harness) + Jev / TypeSafe AI (calibrated risk classifier)

---

## The Pitch

Faah.ai executes real on-call runbooks — the step-by-step incident playbooks SREs
already follow — autonomously for the safe steps, and pauses for human approval
the moment a step is irreversible or Jev scores it as risky.

**One-liner:** An on-call agent that reads the alert email, grades the incident
(low / high / critical), matches it to the right runbook, builds a full
remediation plan with a Jev risk analysis on every step, auto-fixes what's
provably safe, and hard-stops for approval on everything else — then pops the
incident dashboard open the moment the plan is ready.

## Why This Fits the Brief

The brief's three hard requirements, and how we hit each one:

1. **A real tool reached** → MCP tools for logs / metrics / deploy history, plus
   a real email webhook trigger (not a text box) that starts the agent session.
2. **Code run in a sandbox** → TrueForge's sandbox (Daytona) tests every
   remediation script against a staging target before it's allowed near
   "production."
3. **A pause before anything irreversible** → the dual-layer stop mechanism
   below: a hardcoded rule floor, plus Jev's calibrated risk/confidence score.
   Approval is required when *either* layer says so.

## End-to-End Flow

```
Alert email arrives (Mailgun/SendGrid inbound parse)
      │
      ▼
Webhook → backend → enqueue background job, return 200 immediately
      │
      ▼  (background worker)
1. Parse email → extract signals (source, service, alert name, metric, values)
2. Pattern match → map to a runbook (see "Email → Runbook Mapping")
3. Severity triage → LOW / HIGH / CRITICAL (see "Incident Severity")
4. Investigate → read-only MCP tools (get_logs, get_metrics, get_deploy_history)
5. Draft the COMPLETE remediation plan (every step, before running any of them)
6. Score every step: Layer 1 floor → Layer 2 Jev → severity-aware thresholds
7. Push "plan ready" event → dashboard pops up (see "Dashboard Pop-Up")
      │
      ▼
For each step in the plan:
      ├── SAFE (definition below) → sandbox (Daytona) → verify → apply
      └── NOT SAFE → Approval Gate (TrueForge human checkpoint)
                        ├── approved → sandbox → verify → apply
                        └── rejected → skip, mark plan as partially executed
      │
      ▼
Audit trail: email, severity + why, matched runbook + match confidence,
full plan, per-step floor/Jev scores, auto vs escalated, human decisions,
execution + verification results
```

## 1. Incident Severity — LOW / HIGH / CRITICAL

Every incident is graded *before* any action is proposed. Severity decides how
strict the approval thresholds are and who gets notified.

| Severity | Typical signals | Agent behaviour |
|---|---|---|
| **LOW** | Warning-level alert, single non-customer-facing service, error rate < 5%, no user impact, e.g. disk 80% on a worker, elevated latency on an internal job | Investigate + auto-fix SAFE steps. Other steps wait for approval. Notify via dashboard only. |
| **HIGH** | Customer-facing service degraded, error rate 5–25%, p99 latency > 2× baseline, one region/AZ affected, recent deploy correlated | Investigate + auto-fix SAFE steps under *tighter* thresholds. Everything else needs approval. Dashboard pops up + on-call notified. |
| **CRITICAL** | Outage or error rate > 25%, payments/auth/data path affected, multi-service or multi-region, data integrity/security signals | Investigate automatically. **Every remediation step needs human approval**, even if Jev scores it safe. Dashboard pops up immediately with the plan + page on-call. |

How it's decided:
- **Rule pass first:** severity keywords in the email (`CRITICAL`, `P1`, `SEV1`,
  `outage`, `down`), alert-source priority field, service tier from a
  `services.yaml` (tier-0 = payments/auth/checkout).
- **Metric pass:** confirm with `get_metrics` (error rate, latency, affected users).
  Metrics can only *raise* severity, never lower what the email said.
- **Jev pass:** a `severity` Choice question (low/high/critical) with confidence.
  If Jev and the rules disagree, take the **higher** severity. If Jev's
  confidence is < 0.70, bump up one level.
- Severity + the reason for it are shown on the dashboard and written to the audit log.

## 2. Email → Runbook Mapping (Pattern Identification)

The agent must know *which* runbook applies before doing anything.

**Extraction** (from sender, subject and body):
- `source` — Datadog / Grafana / PagerDuty / CloudWatch (from the sender domain)
- `service` — e.g. `checkout-service`
- `alert_name` / `symptom` — e.g. `HighErrorRate`, `PodCrashLoopBackOff`, `DiskPressure`
- `metric` + `value` + `threshold` — e.g. `5xx_rate 40% > 5%`
- `env`, `region`, timestamp

**Matching:** each runbook YAML declares its own trigger patterns:

```yaml
id: high-error-rate
name: Elevated 5xx error rate
triggers:
  alert_names: [HighErrorRate, 5xxSpike]
  subject_patterns: ["5xx", "error rate", "HTTP 50[0-9]"]
  body_keywords: [error_rate, 5xx, status=500]
default_severity: high
steps:
  - id: check-recent-deploys
    action_type: get_deploy_history
  - id: scale-out
    action_type: scale
    template: "scale {service} replicas from {current} to {target}"
  - id: rollback
    action_type: rollback
    template: "rollback {service} to {previous_version}"
```

Matching order:
1. **Exact** — `alert_name` matches a runbook trigger → confidence 1.0
2. **Pattern** — regex/keyword score over subject + body; best score above 0.6 wins
3. **Fallback** — LLM picks from the runbook catalog with a justification; Jev
   scores the match confidence
4. **No match / low confidence** → the `generic-triage` runbook (read-only
   investigation only) + flagged on the dashboard for a human to pick a runbook

The matched runbook, match method and confidence all appear on the dashboard.
Seed runbooks for the demo: `high-error-rate`, `pod-crashloop`, `disk-pressure`,
`db-connection-saturation`, `bad-deploy`.

## 3. Approval Gate — Floor *and* Jev Risk

Human approval is required if **any** of these are true:

1. **Hardcoded floor fires** (`src/hardcoded_floor.py`) → always approval, Jev can't override
2. **Jev risk score ≥ the risk threshold** for this severity
3. **Jev route confidence < the confidence threshold** for this severity
4. **Jev reversibility ≠ `reversible`**, or reversibility confidence < 0.85
5. **Jev route = `human_review` or `hard_escalate`**
6. **Incident severity is CRITICAL** (all remediation steps need approval)
7. **Sandbox run failed or verification didn't pass**

Severity-aware thresholds (show these on screen during the demo):

| Severity | Max Jev risk for auto-execute | Min Jev confidence |
|---|---|---|
| LOW | < 0.30 | > 0.80 |
| HIGH | < 0.20 | > 0.85 |
| CRITICAL | — (never auto) | — |

The approval card shows *why* approval is needed (which rule fired / which Jev
number crossed the line), the Jev risk analysis, the sandbox result, and
Approve / Reject / Edit buttons.

## 4. What "Safe" Means (Auto-Fix Criteria)

A step is **SAFE** and runs without a human only if **all** of these hold:

- **Not floor-blocked** — no hardcoded escalation pattern matches
- **Reversible** — Jev reversibility = `reversible` with confidence > 0.85, and
  the runbook step defines an explicit undo action (e.g. scale 3→8 undo: 8→3)
- **Low risk** — Jev risk score below the severity threshold above
- **Confident** — Jev route = `auto_execute` with confidence above the severity threshold
- **Small blast radius** — touches one stateless service, no data, auth,
  network policy, or stateful store (DB/queue/cache primary)
- **Sandbox-proven** — the step ran cleanly in Daytona against the staging target
  and its verification check passed
- **Severity is not CRITICAL**

Examples:
| Action | Verdict | Why |
|---|---|---|
| `get_logs`, `get_metrics`, `get_deploy_history` | SAFE (no Jev needed) | Read-only |
| Scale stateless service 3 → 8 replicas | SAFE (usually) | Reversible, low risk, one service |
| Restart one crashlooping stateless pod | SAFE (usually) | Reversible, controller recreates it |
| Clear a CDN / app cache | Jev decides | Reversible, but may spike load |
| Rollback a deploy | APPROVAL | Floor rule |
| Restart DB primary / failover | APPROVAL | Floor rule, data-loss risk |
| `DELETE FROM`, `DROP`, `rm -rf`, scale to 0 | APPROVAL | Floor rule |

**Jev risk analysis on every step:** every step in the plan, whether safe or
not, shows the Jev output: risk score, reversibility + confidence, route +
confidence, and a one-line reason. Read-only steps show "read-only — not scored".

## 5. Complete Remediation Plan (Delivered to the User)

After investigation, and before any change is made, the agent produces the full
plan and shows it on the dashboard:

```
Incident #142 — checkout-service 5xx spike (18%)          Severity: HIGH
Runbook: high-error-rate (exact match, 1.00)
Diagnosis: 5xx started 6 min after deploy v2.3.1; pods OOMKilled; memory at 98%

Overall plan risk: CRITICAL (max step risk 0.72) — 2 auto, 1 needs approval

#  Step                                   Floor   Jev risk  Reversible     Decision
1  get_logs / get_metrics / deploys       —       read-only —              ✅ done
2  scale checkout-service 3 → 8           pass    0.08      yes (0.97)     ⚡ auto (sandbox ✓)
3  raise memory limit 512Mi → 1Gi         pass    0.14      yes (0.91)     ⚡ auto (sandbox ✓)
4  rollback checkout-service → v2.3.0     BLOCK   0.72      yes (0.88)     ⏸ approval needed
   Undo: redeploy v2.3.1 · Expected result: 5xx < 1% within 5 min
```

Each step includes: the command, why it's in the plan (linked evidence from
logs/metrics), Jev risk analysis, floor result, sandbox result, the undo action,
the expected outcome + verification check, and its decision (auto / approval).

**Overall plan risk** = the highest step risk as a level (< 0.3 low, < 0.7 high, otherwise critical), raised to at least the incident severity. The plan
can also be exported as Markdown for the post-incident review.

## 6. Dashboard Pop-Up After Background Processing

- The webhook returns 200 immediately and enqueues the job; all the work above
  runs in a **background worker** (Python `asyncio` task or RQ/Celery worker).
- The dashboard subscribes over **SSE/WebSocket**. The worker emits status events:
  `received → parsed → runbook_matched → severity_assigned → investigating →
  plan_ready → executing → awaiting_approval → resolved`.
- On `plan_ready` (or earlier on `severity_assigned` for CRITICAL):
  - the open dashboard switches to the incident view, with a sound + a browser `Notification`
  - if no dashboard tab is open, the backend runs `open http://localhost:3000/#/incident/<id>`
    (macOS) so the dashboard **pops up** by itself for the demo
- The dashboard shows a live timeline of the background steps, then the full
  plan (section 5), then approval cards for anything waiting on a human.

## Differentiation

- Most "AI incident remediation" pitches lean on one LLM to both plan *and*
  self-report its own risk confidence. We split those into two jobs: a
  reasoning model plans, a purpose-built decision model (Jev) gates — cheap
  and fast enough to run on *every* action, not just once.
- A hardcoded rule floor sits underneath Jev so no destructive action can slip
  through purely on a probabilistic score, however confident.
- Severity-aware gating: the same action can auto-run on a LOW incident and
  require approval on a HIGH one; CRITICAL never auto-remediates.
- The user sees the whole plan, with risk per step, *before* anything changes.
- Demo includes a live "try to trick it" moment: a rephrased destructive
  action disguised as safe, to prove the floor catches what a score alone
  might miss.

## Build Priority (one day)

1. Runbook YAML catalog (with `triggers`) + `services.yaml` tiers + mock MCP tools (logs/metrics/deploy history)
2. Email parser + runbook matcher (exact → pattern → LLM fallback → generic-triage)
3. Severity triage (rules + metrics + Jev `severity` question, take the higher)
4. Update `src/router.py`: severity-aware thresholds, use Jev reversibility,
   CRITICAL → always approval, return full per-step risk analysis
5. Plan builder: draft all steps, score each one, compute overall plan risk
6. Sandbox execution wired to a real toy target (+ undo action per step)
7. Approval gate wiring (TrueForge human checkpoint)
8. Email webhook trigger (Mailgun/SendGrid inbound parse) → background worker
9. Dashboard: SSE live timeline, plan view, approval cards, auto pop-up
10. Audit trail view/log
11. Rehearse: LOW auto-resolution → HIGH with 1 approval → CRITICAL hard stop → live trick attempt

## Setup Checklist

```bash
# Scaffold TrueForge (local mode, one command)
npx @truefoundry/trueforge

# Jev SDK
pip install typesafe-sdk
# or: npm install @typesafe-ai/sdk

# TrueForge SDK (to script/automate the agent, e.g. from the email webhook)
npm install @truefoundry/trueforge-sdk
```

Get a TypeSafe AI API key (early access as of Sept 2026) and a model provider
key for TrueForge (Claude/GPT/etc). Confirm the sandbox provider (Daytona) is
configured — check TrueForge's local setup screen for what it asks for.

## KPIs to show judges

- Time from email to dashboard pop-up with a full plan
- Time-to-diagnosis vs. a human
- Runbook match accuracy and severity accuracy across the seed alert emails
- % actions auto-resolved safely vs. escalated, broken down by severity
- Zero false-negative destructive actions (never touches something risky
  without asking — demo this explicitly)

## Implementation notes

- **TrueForge is the agent harness for everything:** the model loop, MCP tool calls, the Daytona sandbox,
  the approval pause (`requireApprovalForTools: ["apply_gated_step"]`) and `ask_user_question`. The orchestrator
  drives it with `@truefoundry/trueforge-sdk`; each incident is one TrueForge session.
- **Server-side enforcement:** the MCP server refuses `apply_safe_step` for anything not cleared, refuses
  `apply_gated_step` without a human approval recorded by the orchestrator, and refuses any step that
  caused data loss in the sandbox, even with approval.
- **Sandbox integrity:** the sandbox script carries a nonce, and the server replays the same test; output
  that doesn't match is rejected.
- **Jev fails closed:** if Jev errors, the step is scored risk 1.0 / hard_escalate, so it needs a human.
- Checkout demo uses an 18% error rate: by the section 1 table, above 25% would be CRITICAL.
