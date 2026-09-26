// One command for the demo: MCP server + TrueForge + (bootstrap) + orchestrator.
import "dotenv/config";
import { spawn } from "node:child_process";

const colors = { mcp: 36, trueforge: 35, bootstrap: 33, orchestrator: 32, "jev-bridge": 34, "llm-relay": 90 };
const children = [];

function run(name, cmd, args) {
  const child = spawn(cmd, args, { env: process.env, stdio: ["ignore", "pipe", "pipe"] });
  const prefix = `\x1b[${colors[name]}m[${name}]\x1b[0m `;
  for (const s of [child.stdout, child.stderr]) {
    s.on("data", (buf) => process.stdout.write(buf.toString().replace(/^(?=.)/gm, prefix)));
  }
  children.push(child);
  return child;
}

async function waitFor(url, label) {
  for (let i = 0; i < 90; i++) {
    try {
      const res = await fetch(url);
      if (res.status < 500) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`${label} did not start (${url})`);
}

function shutdown() {
  for (const c of children) c.kill("SIGTERM");
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

run("mcp", "npm", ["run", "-s", "mcp"]);
if (process.env.LLM_PROVIDER === "custom" && process.env.LLM_RELAY !== "false") run("llm-relay", "npm", ["run", "-s", "llm-relay"]);
run("trueforge", "npm", ["run", "-s", "trueforge"]);
await waitFor(`http://127.0.0.1:${process.env.FAAH_MCP_PORT ?? 8765}/health`, "MCP server");
await waitFor(`${process.env.TRUEFORGE_URL ?? "http://localhost:8790"}/`, "TrueForge");

const boot = run("bootstrap", "npm", ["run", "-s", "bootstrap"]);
const code = await new Promise((r) => boot.on("exit", r));
if (code !== 0) {
  console.error("Bootstrap failed — fix the error above (usually a missing key in .env) and rerun.");
  shutdown();
}
// Local Jev bridge (real Jev via Vercel, or the TrueForge-judge mock) when the SDK is pointed at it.
if ((process.env.TYPESAFE_BASE_URL ?? "").includes("127.0.0.1")) run("jev-bridge", "npm", ["run", "-s", "jev-bridge"]);
run("orchestrator", "npm", ["run", "-s", "orchestrator"]);
