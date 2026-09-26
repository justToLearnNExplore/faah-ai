// Starts TrueForge in local (standalone, SQLite) mode.
// TrueForge's SSRF guard blocks loopback by default; we allowlist exactly
// 127.0.0.1 so the agent can reach the local Faah MCP server and nothing else private.
import "dotenv/config";
import { spawn } from "node:child_process";

const child = spawn("npx", ["trueforge", "--port", process.env.TRUEFORGE_PORT ?? "8790"], {
  stdio: "inherit",
  env: { ...process.env, OUTBOUND_URL_ALLOWED_HOSTS: JSON.stringify(["127.0.0.1"]) },
});
child.on("exit", (code) => process.exit(code ?? 0));
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
