// Orchestrator: webhook listener (tunnel this one) + dashboard/API (localhost only).
import { exec } from "node:child_process";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { config, dashboardUrl } from "./config.js";
import { py } from "./python.js";
import { bus, decide, runtimes } from "./runner.js";
import { trueforgeUp } from "./trueforge.js";
import { webhooks } from "./webhooks.js";

const PUBLIC_DIR = join(import.meta.dirname, "..", "public");
let dashboardClients = 0;

// ------------------------------------------------------------------ dashboard pop-up

bus.on("popup", ({ incidentId, reason }) => {
  const url = `${dashboardUrl()}/#/incident/${encodeURIComponent(incidentId)}`;
  console.log(`[popup] ${incidentId} (${reason})`);
  // Open tabs get an SSE "popup" and switch to the incident themselves.
  // If none is open, open one (macOS `open`, Linux `xdg-open`).
  if (dashboardClients === 0 && config.autoOpen) {
    const opener = process.platform === "darwin" ? "open" : "xdg-open";
    exec(`${opener} "${url}"`);
  }
});

// ------------------------------------------------------------------ API

const api = new Hono();

api.get("/api/health", async (c) =>
  c.json({ trueforge: await trueforgeUp(), mcp: await py.health(), agent: config.agentName }),
);

api.get("/api/incidents", async (c) => {
  const list = await py.listIncidents();
  return c.json(list.map((i) => {
    const rt = runtimes.get(i.id);
    return { ...i, turnStatus: rt?.turnStatus, pending: rt?.pending.filter((p) => !p.decision).length ?? 0 };
  }));
});

api.get("/api/incidents/:id", async (c) => {
  const id = c.req.param("id");
  const incident = await py.getIncident(id);
  const rt = runtimes.get(id);
  return c.json({
    ...incident,
    runtime: rt
      ? { sessionId: rt.sessionId, turnStatus: rt.turnStatus, timeline: rt.timeline, pending: rt.pending, error: rt.error }
      : null,
    trueforgeUrl: config.trueforgeUrl,
  });
});

api.get("/api/incidents/:id/markdown", async (c) => c.text(await py.markdown(c.req.param("id"))));

api.post("/api/incidents/:id/actions/:toolCallId", async (c) => {
  const body = await c.req.json();
  const by = String(body.by || "on-call").slice(0, 60);
  let decision: Parameters<typeof decide>[2];
  if (body.action === "approve") decision = { status: "allow", by };
  else if (body.action === "reject") decision = { status: "deny", by, reason: body.reason || "Rejected by the on-call engineer" };
  else if (body.action === "edit") decision = { status: "deny", by, reason: `Operator requested a change: ${body.reason}` };
  else if (body.action === "answer") decision = { status: "answer", by, answer: String(body.answer ?? "") };
  else return c.json({ error: "action must be approve, reject, edit or answer" }, 400);
  try {
    return c.json(await decide(c.req.param("id"), c.req.param("toolCallId"), decision));
  } catch (err: any) {
    return c.json({ error: err.message }, 409);
  }
});

api.get("/api/cluster", async (c) => c.json(await py.cluster()));
api.post("/api/cluster/reset", async (c) => c.json(await py.resetCluster()));

api.get("/api/stream", (c) =>
  streamSSE(c, async (stream) => {
    dashboardClients++;
    const send = (event: string) => (data: unknown) => stream.writeSSE({ event, data: JSON.stringify(data) });
    const onEvent = send("update");
    const onPopup = send("popup");
    bus.on("event", onEvent);
    bus.on("popup", onPopup);
    stream.onAbort(() => {
      dashboardClients--;
      bus.off("event", onEvent);
      bus.off("popup", onPopup);
    });
    while (!stream.aborted) {
      await stream.writeSSE({ event: "ping", data: "{}" });
      await stream.sleep(15_000);
    }
  }),
);

// ------------------------------------------------------------------ static dashboard

const MIME: Record<string, string> = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".mp3": "audio/mpeg" };

api.get("/*", async (c) => {
  const path = c.req.path === "/" ? "/index.html" : c.req.path;
  if (path.includes("..")) return c.notFound();
  try {
    const body = await readFile(join(PUBLIC_DIR, path));
    const ext = path.slice(path.lastIndexOf("."));
    return c.body(body, 200, { "content-type": MIME[ext] ?? "application/octet-stream" });
  } catch {
    return c.notFound();
  }
});

serve({ fetch: api.fetch, port: config.dashboardPort, hostname: "127.0.0.1" }, () =>
  console.log(`Dashboard:  ${dashboardUrl()}  (localhost only)`),
);
serve({ fetch: webhooks.fetch, port: config.webhookPort }, () =>
  console.log(`Webhooks:   http://localhost:${config.webhookPort}/webhooks/{mailgun,sendgrid,test}  (expose this port via your tunnel)`),
);
