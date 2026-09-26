// Sends a sample alert email through the same webhook pipeline Mailgun/SendGrid use.
//   npm run send -- 3            (sample 3-high-5xx-checkout.json)
//   npm run send -- all          (every sample, 20s apart)
//   npm run send -- 3 --reset    (reset the toy cluster first)
import "dotenv/config";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const dir = join(import.meta.dirname, "..", "samples");
const files = readdirSync(dir).filter((f) => f.endsWith(".json")).sort();
const args = process.argv.slice(2);
const which = args.find((a) => !a.startsWith("--")) ?? "";
const port = process.env.WEBHOOK_PORT ?? "3001";
const token = process.env.FAAH_MCP_TOKEN ?? "";

if (!which) {
  console.log("Usage: npm run send -- <number|all> [--reset]\n");
  for (const f of files) console.log(`  ${f.split("-")[0]}  ${JSON.parse(readFileSync(join(dir, f), "utf8")).scenario}`);
  process.exit(0);
}

if (args.includes("--reset")) {
  await fetch(`http://127.0.0.1:${process.env.DASHBOARD_PORT ?? 3000}/api/cluster/reset`, { method: "POST" });
  console.log("toy cluster reset");
}

const chosen = which === "all" ? files : files.filter((f) => f.startsWith(`${which}-`));
if (!chosen.length) throw new Error(`no sample ${which}`);

for (const [i, f] of chosen.entries()) {
  const sample = JSON.parse(readFileSync(join(dir, f), "utf8"));
  const res = await fetch(`http://127.0.0.1:${port}/webhooks/test`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-faah-token": token },
    body: JSON.stringify(sample),
  });
  console.log(`${f}: ${res.status} ${await res.text()}`);
  if (which === "all" && i < chosen.length - 1) await new Promise((r) => setTimeout(r, 20_000));
}
