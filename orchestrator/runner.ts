// Drives one TrueForge session per incident: starts the turn, streams events,
// turns them into dashboard state, and resumes the turn after human approvals.
import { EventEmitter } from "node:events";
import { isEventDelta, mergeEventDelta } from "@truefoundry/trueforge-sdk";
import { config } from "./config.js";
import { py } from "./python.js";
import { tf, type TrueForgeApi } from "./trueforge.js";

type Event = TrueForgeApi.TurnStreamingEvent;

export type TimelineEntry = { at: string; kind: string; text: string; data?: unknown };

export type Pending = {
  toolCallId: string;
  threadId: string;
  kind: "approval" | "question";
  tool: string;
  args: Record<string, any>;
  stepId?: string;
  requestedAt: string;
  decision?: { status: "allow" | "deny" | "answer"; by: string; reason?: string; answer?: string; at: string };
};

export type Runtime = {
  incidentId: string;
  sessionId?: string;
  turnStatus: "queued" | "running" | "paused" | "done" | "error";
  timeline: TimelineEntry[];
  pending: Pending[];
  agentMessages: string[];
  error?: string;
  popped: Set<string>;
  resumes?: number;
};

// SSE fan-out. `popup` means "bring the dashboard to this incident now".
export const bus = new EventEmitter();
bus.setMaxListeners(100);

export const runtimes = new Map<string, Runtime>();

const STATUS_BY_TOOL: Record<string, string> = {
  parse_alert_email: "parsed",
  match_runbook: "runbook_matched",
  select_runbook: "runbook_matched",
  assess_severity: "severity_assigned",
  get_metrics: "investigating",
  get_logs: "investigating",
  get_deploy_history: "investigating",
  list_pods: "investigating",
  describe_service: "investigating",
  submit_remediation_plan: "plan_ready",
  get_sandbox_test_script: "sandbox_testing",
  record_sandbox_results: "sandbox_tested",
  apply_safe_step: "executing",
  apply_gated_step: "executing",
  verify_incident: "verifying",
};

const now = () => new Date().toISOString();

function push(rt: Runtime, kind: string, text: string, data?: unknown) {
  const entry = { at: now(), kind, text, data };
  rt.timeline.push(entry);
  bus.emit("event", { type: "timeline", incidentId: rt.incidentId, entry });
}

function changed(rt: Runtime) {
  bus.emit("event", { type: "incident", incidentId: rt.incidentId, turnStatus: rt.turnStatus, pending: rt.pending.length });
}

function popup(rt: Runtime, reason: string) {
  if (rt.popped.has(reason)) return;
  rt.popped.add(reason);
  bus.emit("popup", { incidentId: rt.incidentId, reason });
}

function parseJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function textOf(content: TrueForgeApi.ModelMessageEvent["content"]): string {
  if (!content) return "";
  if (typeof content === "string") return content;
  return content.map((part: any) => part.text ?? "").join("");
}

// ------------------------------------------------------------------ queue

let running = 0;
const queue: string[] = [];

export function enqueue(incidentId: string) {
  const rt: Runtime = {
    incidentId,
    turnStatus: "queued",
    timeline: [],
    pending: [],
    agentMessages: [],
    popped: new Set(),
  };
  runtimes.set(incidentId, rt);
  push(rt, "status", "Alert email received — queued for the agent");
  queue.push(incidentId);
  pump();
}

function pump() {
  while (running < config.maxConcurrentIncidents && queue.length) {
    const id = queue.shift()!;
    running++;
    start(runtimes.get(id)!)
      .catch((err) => fail(runtimes.get(id)!, err))
      .finally(() => {
        running--;
        pump();
      });
  }
}

const TRANSIENT = /timeout|cannot connect|upstream failed|ECONNRESET|socket hang up|overloaded|rate limit|\b5\d\d\b/i;
const MAX_RESUMES = 2;

function fail(rt: Runtime, err: any) {
  const message = err?.body ? JSON.stringify(err.body) : String(err?.message ?? err);
  // Transient model/gateway failure: resume the same TrueForge session (it keeps
  // the conversation and tool results) instead of giving up on the incident.
  if (rt.sessionId && TRANSIENT.test(message) && (rt.resumes ?? 0) < MAX_RESUMES) {
    rt.resumes = (rt.resumes ?? 0) + 1;
    push(rt, "error", `Transient model error (${message.slice(0, 160)}) — resuming session, attempt ${rt.resumes}/${MAX_RESUMES}`);
    py.event(rt.incidentId, "agent.resumed", { error: message, attempt: rt.resumes });
    runTurn(rt, [{
      type: "user.message",
      content: `The previous model call failed with a transient error. Continue your procedure for incident_id="${rt.incidentId}" from where you left off. Use get_incident if you need to see what is already done; do not repeat completed steps.`,
    }]).catch((e) => fail(rt, e));
    return;
  }
  rt.turnStatus = "error";
  rt.error = message;
  push(rt, "error", `Agent run failed: ${rt.error}`);
  py.event(rt.incidentId, "agent.error", { error: rt.error }, "agent_error");
  changed(rt);
  popup(rt, "error");
}

// ------------------------------------------------------------------ session + turns

async function start(rt: Runtime) {
  const session = await tf.sessions.create({
    agent: { name: config.agentName },
    metadata: { incident_id: rt.incidentId, source: "faah-orchestrator" },
  });
  rt.sessionId = (session as any).data?.id ?? (session as any).id;
  push(rt, "status", `TrueForge session ${rt.sessionId} started (agent ${config.agentName})`);
  py.event(rt.incidentId, "trueforge.session_created", { session_id: rt.sessionId, agent: config.agentName });

  await runTurn(rt, [
    {
      type: "user.message",
      content: `New incident ${rt.incidentId}: an alert email just arrived. Run your procedure for incident_id="${rt.incidentId}".`,
    },
  ]);
}

async function runTurn(rt: Runtime, input: TrueForgeApi.TurnInputItem[]) {
  rt.turnStatus = "running";
  changed(rt);
  const stream = await tf.sessions.createTurnStream(
    rt.sessionId!,
    { input },
    { timeoutInSeconds: 3600, maxRetries: 0 },
  );
  await consume(rt, stream);
}

type ToolCallInfo = { name: string; args: Record<string, any> };

async function consume(rt: Runtime, stream: AsyncIterable<Event>) {
  const messages = new Map<string, TrueForgeApi.ModelMessageEvent>();
  const toolCalls = new Map<string, ToolCallInfo>();
  const flushed = new Set<string>();

  // Deltas are merged into their base model.message; once any other event
  // arrives the earlier messages are complete and can be read.
  const flush = () => {
    for (const [id, msg] of messages) {
      if (flushed.has(id)) continue;
      flushed.add(id);
      for (const call of msg.toolCalls ?? []) {
        const name = (call as any).toolInfo?.name ?? call.function?.name ?? "unknown";
        toolCalls.set(call.id, { name, args: parseJson(call.function?.arguments ?? "{}") ?? {} });
      }
      const text = textOf(msg.content).trim();
      if (text) {
        rt.agentMessages.push(text);
        push(rt, "agent", text);
      }
    }
  };

  for await (const ev of stream) {
    if (isEventDelta(ev)) {
      const base = messages.get(ev.id);
      if (base) mergeEventDelta(base, ev);
      continue;
    }
    if (ev.type === "model.message") {
      flush();
      messages.set(ev.id, ev);
      continue;
    }
    flush();

    switch (ev.type) {
      case "sandbox.created":
        push(rt, "sandbox", `Daytona sandbox ${ev.sandboxId} created`);
        break;

      case "tool.response": {
        const call = toolCalls.get(ev.toolCallId);
        if (call) await onToolResponse(rt, call, ev.content);
        break;
      }

      case "tool.approval_required":
      case "tool.response_required":
        for (const ref of ev.toolCalls) {
          const call = toolCalls.get(ref.id) ?? { name: "unknown", args: {} };
          addPending(rt, ev.type === "tool.approval_required" ? "approval" : "question", ref.id, ev.threadId, call);
        }
        break;

      case "turn.done": {
        const state = ev.state as any;
        if (state.status === "error") {
          fail(rt, new Error(state.message ?? "turn error"));
        } else if (rt.pending.some((p) => !p.decision)) {
          rt.turnStatus = "paused";
          push(rt, "status", "Paused — waiting for the on-call human");
        } else if (state.status === "cancelled") {
          rt.turnStatus = "error";
          push(rt, "error", "Turn cancelled");
        } else {
          rt.turnStatus = "done";
          push(rt, "status", "Agent finished");
        }
        changed(rt);
        break;
      }
    }
  }
  flush();
}

async function onToolResponse(rt: Runtime, call: ToolCallInfo, content: string) {
  const result = parseJson(content);
  // MCP tool errors arrive as {"error": [{type: "text", text}]}.
  const failed = result?.error !== undefined || (result === undefined && /error|refused|cannot|not safe|no recorded/i.test(content));
  if (result?.error !== undefined) content = [].concat(result.error).map((p: any) => p?.text ?? String(p)).join(" ");
  const label = describeCall(call);

  if (failed) {
    push(rt, "tool-error", `${label} → ${content.slice(0, 400)}`, { tool: call.name });
    return;
  }
  push(rt, "tool", label, { tool: call.name, args: call.args });

  const status = call.name === "resolve_incident" ? result?.outcome : STATUS_BY_TOOL[call.name];
  if (status) changed(rt);

  if (call.name === "assess_severity" && result?.severity === "critical") popup(rt, "critical");
  if (call.name === "submit_remediation_plan") popup(rt, "plan_ready");
  if (call.name === "resolve_incident") push(rt, "status", `Incident ${result?.outcome}: ${result?.summary ?? ""}`);
}

function describeCall(call: ToolCallInfo): string {
  const a = call.args;
  switch (call.name) {
    case "get_metrics":
    case "get_logs":
    case "get_deploy_history":
    case "list_pods":
    case "describe_service":
      return `${call.name}(${a.service})`;
    case "apply_safe_step":
      return `Auto-applied ${a.step_id} (safe)`;
    case "apply_gated_step":
      return `Applied ${a.step_id} after human approval`;
    case "select_runbook":
      return `select_runbook(${a.runbook_id})`;
    default:
      return call.name;
  }
}

function addPending(rt: Runtime, kind: Pending["kind"], toolCallId: string, threadId: string, call: ToolCallInfo) {
  if (rt.pending.some((p) => p.toolCallId === toolCallId)) return;
  const pending: Pending = {
    toolCallId,
    threadId,
    kind,
    tool: call.name,
    args: call.args,
    stepId: call.args.step_id,
    requestedAt: now(),
  };
  rt.pending.push(pending);
  const text =
    kind === "approval"
      ? `Approval needed: ${call.name}${pending.stepId ? ` ${pending.stepId}` : ""}`
      : `Question for on-call: ${call.args.question ?? call.name}`;
  push(rt, kind, text, { toolCallId, args: call.args });
  py.event(rt.incidentId, kind === "approval" ? "approval.requested" : "question.asked",
    { tool: call.name, step_id: pending.stepId, args: call.args }, "awaiting_approval");
  changed(rt);
  popup(rt, `pending:${toolCallId}`);
}

// ------------------------------------------------------------------ human decisions

export async function decide(
  incidentId: string,
  toolCallId: string,
  decision: { status: "allow" | "deny" | "answer"; by: string; reason?: string; answer?: string },
) {
  const rt = runtimes.get(incidentId);
  if (!rt) throw new Error("unknown incident (orchestrator restarted?)");
  const pending = rt.pending.find((p) => p.toolCallId === toolCallId && !p.decision);
  if (!pending) throw new Error("no pending action with that id");

  if (pending.kind === "approval" && pending.tool === "apply_gated_step") {
    // Record the decision on the MCP server first: apply_gated_step refuses without it.
    await py.recordApproval(incidentId, {
      step_id: pending.stepId!,
      decision: decision.status === "allow" ? "allow" : "deny",
      by: decision.by,
      reason: decision.reason,
    });
  }
  pending.decision = { ...decision, at: now() };
  const verb = decision.status === "answer" ? `answered "${decision.answer}"` : decision.status === "allow" ? "approved" : `rejected${decision.reason ? ` — ${decision.reason}` : ""}`;
  push(rt, "human", `${decision.by} ${verb}${pending.stepId ? ` (${pending.stepId})` : ""}`);
  py.event(incidentId, `human.${decision.status}`, { tool: pending.tool, step_id: pending.stepId, ...decision });
  changed(rt);

  // Resume only when every action in this pause has a decision.
  const open = rt.pending.filter((p) => !p.decision);
  if (open.length) return { resumed: false, waitingOn: open.length };

  const input: TrueForgeApi.TurnInputItem[] = rt.pending
    .filter((p) => p.decision && !(p as any).sent)
    .map((p) => {
      (p as any).sent = true;
      if (p.kind === "question") {
        return { type: "user.tool_response", threadId: p.threadId, toolCallId: p.toolCallId, content: p.decision!.answer ?? "" };
      }
      return {
        type: "user.tool_approval",
        threadId: p.threadId,
        toolCallId: p.toolCallId,
        approval: p.decision!.status === "allow"
          ? { status: "allow" }
          : { status: "deny", reason: p.decision!.reason ?? "Rejected by the on-call engineer" },
      };
    });
  rt.pending = rt.pending.filter((p) => !(p as any).sent);

  runTurn(rt, input).catch((err) => fail(rt, err));
  return { resumed: true };
}
