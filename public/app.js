// Faah.ai dashboard — vanilla JS, live over SSE. All incident text is escaped:
// alert emails are untrusted input.

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const hhmmss = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "");

const STAGES = [
  ["received", "Email received"],
  ["parsed", "Parsed"],
  ["runbook_matched", "Runbook"],
  ["severity_assigned", "Severity"],
  ["investigating", "Investigating"],
  ["plan_ready", "Plan ready"],
  ["sandbox_tested", "Sandbox tested"],
  ["executing", "Executing"],
  ["closed", "Closed"],
];
const STAGE_INDEX = {
  received: 0, parsed: 1, runbook_matched: 2, severity_assigned: 3, investigating: 4, plan_ready: 5,
  sandbox_testing: 5, sandbox_tested: 6, executing: 7, awaiting_approval: 7, verifying: 7,
  resolved: 8, partially_resolved: 8, escalated: 8, agent_error: 8,
};
const CLOSED = new Set(["resolved", "partially_resolved", "escalated", "agent_error"]);

let currentId = null;
let refreshTimer = null;

// ------------------------------------------------------------------ data

async function api(path, init) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status}`);
  return body;
}

async function loadList() {
  const list = await api("/api/incidents").catch(() => []);
  $("#incident-list").innerHTML = list.length
    ? list.map((i) => `
      <li><a href="#/incident/${encodeURIComponent(i.id)}" ${i.id === currentId ? 'aria-current="true"' : ""}>
        <span class="pip ${i.severity ? `sev-${esc(i.severity)}` : ""}"></span>
        <span class="subj">${esc(i.subject)}</span>
        <span class="meta">${esc(i.service ?? "unknown service")} · ${esc(label(i.status))}
          ${i.pending ? `· <span class="needs">${i.pending} waiting on you</span>` : ""}</span>
      </a></li>`).join("")
    : `<li class="muted" style="padding:0 16px">No incidents yet.</li>`;
}

function label(status) {
  return ({ runbook_matched: "runbook matched", severity_assigned: "severity assigned", plan_ready: "plan ready",
    sandbox_testing: "sandbox testing", sandbox_tested: "sandbox tested", awaiting_approval: "waiting on you",
    partially_resolved: "partially resolved", agent_error: "agent error" })[status] ?? status ?? "";
}

async function loadHealth() {
  const h = await api("/api/health").catch(() => ({}));
  $("#health").innerHTML = [["TrueForge", h.trueforge], ["MCP tools", h.mcp]]
    .map(([n, up]) => `<span><span class="dot ${up ? "up" : ""}"></span>${n}</span>`).join("");
}

// ------------------------------------------------------------------ render incident

async function showIncident(id, { flash = false } = {}) {
  currentId = id;
  let inc;
  try {
    inc = await api(`/api/incidents/${encodeURIComponent(id)}`);
  } catch (err) {
    $("#main").innerHTML = `<section class="empty"><h1>Incident not found</h1><p>${esc(err.message)}</p></section>`;
    return;
  }
  const sev = inc.severity?.severity;
  const rt = inc.runtime || {};
  const pending = (rt.pending || []).filter((p) => !p.decision);
  const stageNow = STAGE_INDEX[inc.status] ?? 0;
  const closed = CLOSED.has(inc.status);

  $("#main").innerHTML = `
    <div class="inc-head">
      <div class="lamp ${sev ? `sev-${esc(sev)}` : "unlit"} ${flash ? "flash" : ""}" role="status">
        ${sev ? esc(sev) : "triage"}<small>${sev ? "severity" : "grading…"}</small>
      </div>
      <div class="inc-title">
        <div class="id">${esc(inc.id)} · ${esc(inc.received_via)} · ${esc(hhmmss(inc.created_at))}</div>
        <h1>${esc(inc.email.subject)}</h1>
        <div class="facts">
          <span>Service <b>${esc(inc.parsed?.service ?? "—")}</b></span>
          <span>Runbook <b>${esc(inc.runbook_id ?? "—")}</b>${inc.runbook_match ? ` <span class="muted">(${esc(inc.runbook_match.method)}${inc.runbook_match.confidence != null ? `, ${num(inc.runbook_match.confidence)}` : ""})</span>` : ""}</span>
          <span>Status <b>${esc(label(inc.status))}</b></span>
          <span>Agent <b>${esc(rt.turnStatus ?? "—")}</b>${rt.sessionId ? ` <a class="muted" href="${esc(inc.trueforgeUrl)}" target="_blank" rel="noopener">TrueForge session ${esc(rt.sessionId.slice(-6))}</a>` : ""}</span>
        </div>
      </div>
    </div>

    <ol class="pipeline" aria-label="Progress">
      ${STAGES.map(([, name], i) => `<li class="${i < stageNow || (closed && i === stageNow) ? "done" : i === stageNow ? "now" : ""}">${esc(i === 8 && closed ? label(inc.status) : name)}</li>`).join("")}
    </ol>

    ${pending.length ? renderPending(inc, pending) : ""}
    ${inc.resolution ? `<section class="panel"><h2>Outcome <span class="aside">${esc(label(inc.resolution.outcome))}</span></h2><p>${esc(inc.resolution.summary)}</p>${renderChecks(inc.verification)}</section>` : ""}
    ${renderPlan(inc)}

    <div class="two-col">
      <section class="panel"><h2>Live timeline <span class="aside">TrueForge session events</span></h2>
        <ol class="timeline" id="timeline">${timelineOf(inc).map(renderEntry).join("") || `<li><span></span><span class="muted">Waiting for the agent…</span></li>`}</ol>
      </section>
      <div>
        ${renderSeverity(inc)}
        <section class="panel"><h2>Alert email <span class="aside">untrusted input</span></h2>
          <div class="muted" style="font-size:12px">${esc(inc.email.sender)}</div>
          <pre class="email-body">${esc(inc.email.body)}</pre>
        </section>
      </div>
    </div>

    <section class="panel">
      <h2>Audit trail <span class="aside">${(inc.audit || []).length} events</span></h2>
      <p><a class="btn ghost small" href="/api/incidents/${encodeURIComponent(inc.id)}/markdown" target="_blank">Export plan as Markdown</a></p>
      <details><summary class="muted">Show every event</summary>
        <ol class="timeline">${(inc.audit || []).map((a) => `<li><time>${esc(hhmmss(a.at))}</time><span><b>${esc(a.event)}</b> <span class="muted mono">${esc(JSON.stringify(a.data).slice(0, 220))}</span></span></li>`).join("")}</ol>
      </details>
    </section>`;

  bindActions(inc);
  const tl = $("#timeline");
  if (tl) tl.scrollTop = tl.scrollHeight;
  document.title = pending.length ? `(${pending.length}) Faah.ai — ${inc.parsed?.service ?? inc.id}` : `Faah.ai — ${inc.parsed?.service ?? inc.id}`;
  loadList();
}

// Live TrueForge events while the orchestrator holds the session; the persisted
// audit trail otherwise (e.g. after an orchestrator restart).
function timelineOf(inc) {
  if (inc.runtime?.timeline?.length) return inc.runtime.timeline;
  return (inc.audit || []).map((a) => ({ at: a.at, kind: a.event.startsWith("human") ? "human" : "tool", text: a.event.replaceAll("_", " ") }));
}

function renderEntry(e) {
  const text = e.kind === "sandbox" ? e.text.replace(/^Daytona sandbox v1:local:.*\/[^/]*([^/]{8}) created$/, "TrueForge local sandbox created ($1)") : e.text;
  return `<li><time>${esc(hhmmss(e.at))}</time><span class="k-${esc(e.kind)}">${esc(text)}</span></li>`;
}

function renderPending(inc, pending) {
  const steps = inc.plan?.steps || [];
  return `<section class="panel attention" aria-live="assertive">
    <h2>Waiting on you <span class="aside">the agent is paused until you decide</span></h2>
    ${pending.map((p) => {
      if (p.kind === "question") {
        const options = p.args.options || [];
        return `<div class="card" data-call="${esc(p.toolCallId)}">
          <h3>${esc(p.args.question)}</h3>
          <div class="options">${options.map((o) => `<button class="btn ghost" data-answer="${esc(o)}">${esc(o)}</button>`).join("")}</div>
          <div class="row"><input type="text" placeholder="Or type an answer" aria-label="Answer" /><button class="btn" data-act="answer">Send answer</button></div>
        </div>`;
      }
      const step = steps.find((s) => s.step_id === p.stepId);
      const r = step?.routing || {};
      return `<div class="card" data-call="${esc(p.toolCallId)}">
        <h3>${esc(p.stepId ?? "")} · ${esc(step?.description ?? p.tool)}</h3>
        ${step ? `<div class="row">${gauge(step, inc.plan)}</div>` : ""}
        <ul class="why">${(r.reasons || ["TrueForge requires approval for this tool"]).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${step?.sandbox ? `<div class="muted" style="font-size:13px">Sandbox: ${step.sandbox.passed ? "passed" : `failed — ${esc(step.sandbox.reasons.join("; "))}`}. Undo: ${esc(undoText(step.undo))}</div>` : ""}
        <div class="row">
          <button class="btn approve" data-act="approve">Approve and run</button>
          <button class="btn reject" data-act="reject">Reject</button>
          <input type="text" placeholder="Reason, or the change you want" aria-label="Reason" />
          <button class="btn ghost" data-act="edit">Request change</button>
        </div>
      </div>`;
    }).join("")}
  </section>`;
}

function undoText(undo) {
  if (!undo) return "none — cannot be undone";
  if (undo.action_type === "none_needed") return undo.note ?? "not needed";
  return `${undo.action_type} ${JSON.stringify(undo.params)}`;
}

function gauge(step, plan) {
  const jev = step.routing?.jev;
  if (!jev) return `<span class="muted" style="font-size:12px">${step.decision === "read_only" ? "read-only — not scored" : "not scored"}</span>`;
  const thr = plan?.thresholds;
  const thrPct = thr ? thr.max_risk * 100 : 0;
  return `<div class="gauge" style="width:100%">
    <div class="legend"><span>Jev risk <b>${num(jev.risk_score)}</b></span><span>${thr ? `auto below ${num(thr.max_risk)}` : "critical: never auto"}</span></div>
    <div class="track ${thr ? "" : "never"}" style="--thr:${thrPct}%;--risk:${Math.min(100, jev.risk_score * 100)}%" role="img"
      aria-label="Jev risk ${num(jev.risk_score)}${thr ? `, auto-execute threshold ${num(thr.max_risk)}` : ""}">
      ${thr ? `<span class="thr"></span>` : ""}<span class="needle"></span>
    </div>
    <div class="legend"><span>${esc(jev.reversibility)} ${num(jev.reversibility_confidence)}</span><span>route ${esc(jev.route)} ${num(jev.route_confidence)}</span></div>
  </div>`;
}

function renderPlan(inc) {
  const plan = inc.plan;
  if (!plan) return `<section class="panel"><h2>Remediation plan</h2><p class="muted">The agent is still investigating. The complete plan appears here, with a Jev risk analysis on every step, before anything is changed.</p></section>`;
  const c = plan.counts;
  return `<section class="panel">
    <h2>Remediation plan <span class="aside">submitted ${esc(hhmmss(plan.submitted_at))}</span></h2>
    <p><b>Diagnosis:</b> ${esc(plan.diagnosis)}</p>
    <div class="plan-summary">
      <span>Overall plan risk <b class="chip ${plan.overall_risk.label === "low" ? "auto" : "gate"}">${esc(plan.overall_risk.label)}</b> (max step risk ${num(plan.overall_risk.max_step_risk)})</span>
      <span><b>${c.auto_execute}</b> auto</span><span><b>${c.needs_approval}</b> need approval</span>
      ${c.invalid ? `<span><b>${c.invalid}</b> invalid</span>` : ""}
      ${scorerLabel(plan)}
      <span>Thresholds (${esc(plan.severity)}): ${plan.thresholds ? `risk &lt; ${plan.thresholds.max_risk}, confidence &gt; ${plan.thresholds.min_confidence}` : "none — every step needs approval"}</span>
    </div>
    <ol class="steps">${plan.steps.map((s) => renderStep(s, plan)).join("")}</ol>
    ${plan.projected ? `<p class="muted" style="font-size:13px">Projected after all sandbox-passing steps: ${Object.entries(plan.projected).map(([n, m]) => `${esc(n)} error ${pct(m.error_rate)}${m.p99_ms ? `, p99 ${esc(m.p99_ms)}ms` : ""}${m.disk_used_pct != null ? `, disk ${esc(m.disk_used_pct)}%` : ""}${m.connection_util != null ? `, conns ${pct(m.connection_util)}` : ""} (${esc(m.status)})`).join("; ")}</p>` : ""}
  </section>`;
}

function scorerLabel(plan) {
  const model = plan.steps.map((s) => s.routing?.jev?.model).find(Boolean);
  if (!model) return "";
  const mock = model.startsWith("jev-mock");
  return `<span>Risk scorer <b class="chip ${mock ? "gate" : "auto"}" title="${esc(model)}">${mock ? "Jev mock · TrueForge judge" : `Jev · ${esc(model)}`}</b></span>`;
}

function renderStep(s, plan) {
  const r = s.routing || {};
  const floorHit = r.floor?.floor_triggered;
  const decisionChip = { auto_execute: `<span class="chip auto">Auto</span>`, needs_approval: `<span class="chip gate">Needs approval</span>`,
    read_only: `<span class="chip read">Read-only</span>`, invalid: `<span class="chip gate">Invalid</span>` }[s.decision];
  const exec = s.execution;
  const execText = !exec ? "Not run" : exec.status === "applied" ? (exec.mode === "auto" ? "Auto-applied" : `Applied — approved by ${exec.approved_by}`) : `Rejected${exec.reason ? `: ${exec.reason}` : ""}`;
  const sb = s.sandbox;
  return `<li class="step">
    <span class="sid">${esc(s.step_id)}</span>
    <div><div class="desc">${esc(s.description)}</div><div class="sub">${esc(s.title)}${s.params?.command ? ` · <code>${esc(s.params.command)}</code>` : ""}</div></div>
    ${gauge(s, plan)}
    <div class="status-col">
      ${decisionChip}${floorHit ? `<span class="chip stamp">Floor</span>` : ""}
      <span>Sandbox: ${sb ? (sb.passed ? "passed" : `<b style="color:var(--critical)">failed</b>`) : "—"}</span>
      <span>${esc(execText)}</span>
    </div>
    <details><summary>Why, evidence and undo</summary><dl>
      ${r.reasons?.length ? `<dt>Needs approval because</dt><dd><ul class="why" style="margin:0">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></dd>` : ""}
      <dt>Rationale</dt><dd>${esc(s.rationale ?? "—")}</dd>
      <dt>Evidence</dt><dd>${esc(s.evidence ?? "—")}</dd>
      <dt>Expected outcome</dt><dd>${esc(s.expected_outcome ?? "—")}</dd>
      <dt>Undo</dt><dd>${esc(undoText(s.undo))}</dd>
      ${r.jev ? `<dt>Jev</dt><dd>${esc(r.jev.reason)}${r.jev.latency_ms != null ? ` · ${esc(r.jev.latency_ms)}ms` : ""}</dd>` : ""}
      ${floorHit ? `<dt>Hardcoded floor</dt><dd>${esc(r.floor.reason)}</dd>` : ""}
      ${sb ? `<dt>Sandbox</dt><dd>before error ${pct(sb.before.error_rate)} → after ${pct(sb.after.error_rate)}${sb.reasons.length ? ` · ${esc(sb.reasons.join("; "))}` : ""}</dd>` : ""}
      ${s.problems?.length ? `<dt>Problems</dt><dd>${esc(s.problems.join("; "))}</dd>` : ""}
    </dl></details>
  </li>`;
}

function renderSeverity(inc) {
  const s = inc.severity;
  if (!s) return "";
  const passes = s.passes;
  return `<section class="panel"><h2>Why ${esc(s.severity)}? <span class="aside">the highest of three passes wins</span></h2>
    <ul class="sev-reasons">
      <li><b>Rules → ${esc(passes.rules.level)}.</b> ${esc(passes.rules.reasons.join("; ") || "no signals")}</li>
      <li><b>Metrics → ${esc(passes.metrics.level)}.</b> ${esc(passes.metrics.reasons.join("; ") || "within limits")}</li>
      <li><b>Jev → ${esc(passes.jev.level ?? "unavailable")}.</b> ${esc(passes.jev.reasons.join("; "))}</li>
    </ul></section>`;
}

function renderChecks(v) {
  if (!v?.checks?.length) return "";
  return `<table class="checks"><thead><tr><th>Service</th><th>Check</th><th>Value</th><th>Limit</th><th></th></tr></thead><tbody>
    ${v.checks.map((c) => `<tr><td>${esc(c.service)}</td><td>${esc(c.metric)}</td><td class="mono">${esc(c.value)}</td><td class="mono">${esc(c.limit)}</td><td>${c.ok ? "✓" : "✗"}</td></tr>`).join("")}
  </tbody></table>`;
}

function bindActions(inc) {
  document.querySelectorAll(".card[data-call]").forEach((card) => {
    const call = card.dataset.call;
    const input = card.querySelector("input");
    const send = async (payload) => {
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await api(`/api/incidents/${encodeURIComponent(inc.id)}/actions/${encodeURIComponent(call)}`, {
          method: "POST", body: JSON.stringify({ by: whoAmI(), ...payload }),
        });
      } catch (err) {
        alert(`Could not send your decision: ${err.message}`);
      }
      showIncident(inc.id);
    };
    card.querySelectorAll("[data-answer]").forEach((b) => b.addEventListener("click", () => send({ action: "answer", answer: b.dataset.answer })));
    card.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
      const act = b.dataset.act;
      if ((act === "edit" || act === "answer") && !input.value.trim()) { input.focus(); return; }
      send(act === "answer" ? { action: "answer", answer: input.value } : { action: act, reason: input.value });
    }));
  });
}

function whoAmI() {
  let name = null;
  try { name = localStorage.getItem("faah-oncall-name"); } catch {}
  if (!name) {
    name = (prompt("Your name for the audit trail:", "on-call") || "on-call").slice(0, 60);
    try { localStorage.setItem("faah-oncall-name", name); } catch {}
  }
  return name;
}

// ------------------------------------------------------------------ pop-up + live updates

function chime() {
  try {
    const ctx = new AudioContext();
    [880, 660].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.15, ctx.currentTime + i * 0.18);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.18 + 0.16);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.18);
      o.stop(ctx.currentTime + i * 0.18 + 0.17);
    });
  } catch {}
}

function onPopup({ incidentId, reason }) {
  const text = reason === "critical" ? "CRITICAL incident — every fix needs your approval"
    : reason === "plan_ready" ? "Remediation plan ready"
    : reason === "error" ? "Agent run failed"
    : "Waiting on your decision";
  chime();
  if ("Notification" in window && Notification.permission === "granted") {
    const n = new Notification(`Faah.ai · ${incidentId}`, { body: text, tag: incidentId });
    n.onclick = () => { window.focus(); location.hash = `#/incident/${incidentId}`; };
  }
  window.focus();
  if (location.hash !== `#/incident/${incidentId}`) location.hash = `#/incident/${incidentId}`;
  else showIncident(incidentId, { flash: true });
}

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("popup", (e) => onPopup(JSON.parse(e.data)));
  es.addEventListener("update", (e) => {
    const msg = JSON.parse(e.data);
    if (msg.incidentId === currentId) {
      // Coalesce bursts of events into one re-render.
      clearTimeout(refreshTimer);
      const rerender = () => {
        // Don't wipe a reason/answer the on-call human is typing.
        const typing = [...document.querySelectorAll("#main input")].some((i) => i.value || i === document.activeElement);
        if (typing) refreshTimer = setTimeout(rerender, 1500);
        else showIncident(currentId);
      };
      refreshTimer = setTimeout(rerender, 250);
    } else {
      loadList();
    }
  });
  es.onerror = () => loadHealth();
}

function route() {
  const m = location.hash.match(/^#\/incident\/(.+)$/);
  if (m) showIncident(decodeURIComponent(m[1]), { flash: true });
  else {
    currentId = null;
    $("#main").replaceChildren($("#empty-tpl").content.cloneNode(true));
    loadList();
  }
}

$("#alerts-btn").addEventListener("click", async () => {
  if ("Notification" in window) await Notification.requestPermission();
  chime();
  $("#alerts-btn").textContent = Notification.permission === "granted" ? "Alerts on" : "Alerts blocked";
});
$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Reset the demo cluster to its faulty starting state?")) return;
  await api("/api/cluster/reset", { method: "POST" });
});

window.addEventListener("hashchange", route);
loadHealth();
setInterval(loadHealth, 20_000);
connect();
route();
