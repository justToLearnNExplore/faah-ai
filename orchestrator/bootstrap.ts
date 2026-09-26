// One-time (idempotent) TrueForge setup: model provider, Daytona sandbox,
// the Faah MCP server connector, and the faah-oncall agent.
import { agentSpec } from "./agent.js";
import { config, resourceName } from "./config.js";
import { tf, trueforgeUp } from "./trueforge.js";

async function step(label: string, fn: () => Promise<unknown>) {
  process.stdout.write(`• ${label} ... `);
  await fn();
  console.log("ok");
}

export async function bootstrap() {
  if (!(await trueforgeUp())) {
    throw new Error(`TrueForge is not reachable at ${config.trueforgeUrl} — start it with: npm run trueforge`);
  }

  const models = [
    { name: resourceName(config.modelId), modelId: config.modelId, properties: {} },
    // Small, fast model for the Jev mock's judge sessions.
    ...(config.jevMockModelId !== config.modelId
      ? [{ name: resourceName(config.jevMockModelId), modelId: config.jevMockModelId, properties: {} }]
      : []),
  ];
  await step(`model provider ${config.providerName} (${config.modelId}, ${config.jevMockModelId})`, () =>
    tf.settings.modelProviders.createOrUpdate({
      manifest: config.provider === "custom"
        ? {
            type: "custom",
            name: config.providerName,
            // Via the local relay, which retries hung gateway requests (see llm-relay.ts).
            baseUrl: config.useLlmRelay ? config.llmRelayUrl : config.required("CUSTOM_PROVIDER_BASE_URL"),
            auth: { apiKey: config.providerApiKey() },
            models,
          }
        : { type: config.provider, auth: { apiKey: config.providerApiKey() }, models },
    }),
  );
  if (process.env.DAYTONA_API_KEY) {
    await step("sandbox provider daytona", () =>
      tf.settings.sandboxProviders.createOrUpdate({
        manifest: {
          type: "daytona",
          auth: { apiKey: config.required("DAYTONA_API_KEY") },
          autoStopIntervalInMinutes: 15,
          autoArchiveIntervalInMinutes: 60,
          autoDeleteIntervalInMinutes: 120,
          execTimeoutMs: 60_000,
        },
      }),
    );
  } else {
    console.log("• sandbox provider: DAYTONA_API_KEY not set — TrueForge will use its local sandbox (sandbox-runtime)");
  }

  await step(`MCP server ${config.mcpServerName} -> ${config.mcpUrl}/mcp`, () =>
    tf.settings.mcpServers.createOrUpdate({
      manifest: {
        type: "remote",
        name: config.mcpServerName,
        description:
          "Faah.ai incident tools: alert parsing, runbook matching, severity triage, read-only investigation, " +
          "remediation planning with floor + Jev risk scoring, sandbox verification and gated execution.",
        url: `${config.mcpUrl}/mcp`,
        auth: { type: "header", headers: { "X-Faah-Token": config.mcpToken() } },
      },
    }),
  );

  await step(`MCP tools visible to TrueForge`, async () => {
    const tools = await tf.mcpServers.listTools(config.mcpServerName);
    const names = JSON.stringify(tools).match(/"name":"[a-z_]+"/g) ?? [];
    if (!names.some((n) => n.includes("apply_gated_step"))) {
      throw new Error(`TrueForge could not list the Faah tools. Is the MCP server running (npm run mcp)? Got: ${JSON.stringify(tools).slice(0, 300)}`);
    }
  });

  const description = "Faah.ai on-call runbook executor: triage, plan, sandbox-test, auto-fix safe steps, gate the rest.";
  await step(`agent ${config.agentName}`, async () => {
    const existing = [];
    for await (const agent of await tf.agents.list()) existing.push(agent);
    const found = existing.find((a: any) => a.name === config.agentName);
    if (found) {
      await tf.agents.update((found as any).id, { description, manifest: agentSpec() });
    } else {
      await tf.agents.create({ name: config.agentName, description, manifest: agentSpec() });
    }
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  bootstrap()
    .then(() => console.log("\nTrueForge is configured for Faah.ai."))
    .catch((err) => {
      console.error("\nBootstrap failed:", err?.body ? JSON.stringify(err.body) : err.message);
      process.exit(1);
    });
}
