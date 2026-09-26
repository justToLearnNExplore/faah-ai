// Contract test: the REAL typesafe-sdk (via src/jev_classifier.py) talks to the Jev bridge (mock backend)
// and parses its answers. The TrueForge judge is replaced by a fixed sampler here, so this
// runs offline; the live path is exercised by `npm run dev`.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { serve } from "@hono/node-server";

// Pin the mock backend before the module reads its config, so this never calls Vercel.
process.env.JEV_BACKEND = "trueforge-judge";
const { app, deps } = await import("../orchestrator/jev-bridge.js");

const PORT = 18766;
const KEY = "contract-test-key";
process.env.TYPESAFE_API_KEY = KEY;

// Three "samples" that mostly agree: safe, reversible, auto_execute.
let n = 0;
deps.sample = async (_state: unknown, questions: Record<string, unknown>) => {
  n++;
  const answer: Record<string, Record<string, number>> = {};
  for (const [name, q] of Object.entries(questions) as [string, any][]) {
    if (q.type === "choice") {
      const labels = Object.keys(q.criteria);
      answer[name] = Object.fromEntries(labels.map((l, i) => [l, i === 0 ? (n === 3 ? 0.4 : 0.8) : 0.2 / (labels.length - 1)]));
    } else if (q.type === "score") {
      answer[name] = Object.fromEntries(q.criteria.map((_: unknown, i: number) => [String(i), i === 0 ? 0.6 : i === 1 ? 0.3 : 0.1 / (q.criteria.length - 2)]));
    } else {
      answer[name] = { true: 0.9, false: 0.1 };
    }
  }
  return { answer, usage: { input: 100, output: 20 } };
};

const server = serve({ fetch: app.fetch, port: PORT, hostname: "127.0.0.1" });
await new Promise((r) => setTimeout(r, 300));

const py = `
import json, jev_classifier as j
a = j.classify_action("ctx", "scale search-service replicas to 8", {"service": "search-service"})
s = j.classify_severity({"alert": "x"})
f = j.score_runbook_fit({"alert": "x"}, {"id": "high-latency"}, "because")
print(json.dumps({"action": a, "severity": s, "fit": f}))
`;
// Async: the mock server runs in this process, so the event loop must stay free.
let stdout: string;
try {
  ({ stdout } = await promisify(execFile)("../.venv/bin/python", ["-c", py], {
    cwd: "src",
    env: { ...process.env, TYPESAFE_BASE_URL: `http://127.0.0.1:${PORT}`, TYPESAFE_API_KEY: KEY, PYTHONPATH: "." },
    timeout: 30_000,
  }));
} finally {
  server.close();
}
const r = JSON.parse(stdout.trim().split("\n").pop()!);

const checks: [string, boolean][] = [
  ["no fail-closed error", !r.action.error && !r.severity.error && !r.fit.error],
  ["reversibility parsed", r.action.reversibility === "reversible" && r.action.reversibility_confidence > 0.5],
  ["route parsed", r.action.route === "auto_execute" && r.action.route_confidence < 1],
  ["risk in [0,1]", r.action.risk_score >= 0 && r.action.risk_score <= 1],
  ["severity parsed", r.severity.severity === "low" && r.severity.confidence > 0],
  ["noul parsed", r.fit.fit > 0.8],
  ["model label", String(r.action.model).startsWith("jev-mock")],
];
for (const [name, ok] of checks) console.log(`${ok ? "✓" : "✗"} ${name}`);
console.log(JSON.stringify(r.action));
if (checks.some(([, ok]) => !ok)) process.exit(1);
