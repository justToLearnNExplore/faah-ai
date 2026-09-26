import "dotenv/config";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set — copy .env.example to .env and fill it in`);
  return value;
}

const DEFAULT_MODELS = {
  openai: { agent: "gpt-4.1", judge: "gpt-4.1-mini" },
  anthropic: { agent: "claude-sonnet-5", judge: "claude-haiku-4-5" },
  custom: { agent: "vm-polaris/openai", judge: "openai-polaris/gpt-4.1-mini" },
};

type Provider = "openai" | "anthropic" | "custom";

function provider(): Provider {
  const p = (process.env.LLM_PROVIDER ?? "openai").toLowerCase();
  if (p !== "openai" && p !== "anthropic" && p !== "custom") {
    throw new Error(`LLM_PROVIDER must be openai, anthropic or custom, got ${p}`);
  }
  return p;
}

/** TrueForge resource names are lowercase letters, digits and hyphens ("gpt-4.1" -> "gpt-4-1"). */
export const resourceName = (id: string) => id.toLowerCase().replace(/[^a-z0-9-]+/g, "-");

export const config = {
  trueforgeUrl: process.env.TRUEFORGE_URL ?? "http://localhost:8790",
  trueforgeApiKey: process.env.TRUEFORGE_API_KEY ?? "trueforge-standalone",
  agentName: process.env.TRUEFORGE_AGENT_NAME ?? "faah-oncall",
  // LLM provider for the agent and the Jev mock's judge: "openai", "anthropic", or "custom"
  // (any OpenAI-compatible endpoint, e.g. the TrueFoundry AI gateway).
  provider: provider(),
  providerName: provider() === "custom" ? resourceName(process.env.CUSTOM_PROVIDER_NAME || "gpt-model") : provider(),
  // TrueForge talks to the gateway through the local retrying relay (orchestrator/llm-relay.ts).
  useLlmRelay: (process.env.LLM_RELAY ?? "true") !== "false",
  llmRelayUrl: `http://127.0.0.1:${process.env.LLM_RELAY_PORT ?? "8767"}`,
  modelId: process.env.TRUEFORGE_MODEL_ID || DEFAULT_MODELS[provider()].agent,
  jevMockModelId: process.env.JEV_MOCK_MODEL_ID || DEFAULT_MODELS[provider()].judge,
  providerApiKey: () => required({ openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY", custom: "CUSTOM_PROVIDER_API_KEY" }[provider()]),
  mcpServerName: "faah-incident-tools",
  // 127.0.0.1, not localhost: TrueForge's outbound guard allowlists this exact host.
  mcpUrl: `http://127.0.0.1:${process.env.FAAH_MCP_PORT ?? "8765"}`,
  mcpToken: () => required("FAAH_MCP_TOKEN"),
  dashboardPort: Number(process.env.DASHBOARD_PORT ?? 3000),
  webhookPort: Number(process.env.WEBHOOK_PORT ?? 3001),
  autoOpen: (process.env.AUTO_OPEN_DASHBOARD ?? "true") !== "false",
  mailgunSigningKey: process.env.MAILGUN_WEBHOOK_SIGNING_KEY ?? "",
  sendgridSecret: process.env.SENDGRID_INBOUND_SECRET ?? "",
  maxConcurrentIncidents: 2,
  required,
};



/** TrueForge model FQN: provider/<configured model name>. */
export const modelName = (id: string) => `${config.providerName}/${resourceName(id)}`;

export const dashboardUrl = () => `http://localhost:${config.dashboardPort}`;
