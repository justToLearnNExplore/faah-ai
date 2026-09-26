// Client for the Faah MCP server's /internal HTTP routes (incident state lives there).
import { config } from "./config.js";

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${config.mcpUrl}${path}`, {
    ...init,
    headers: { "content-type": "application/json", "x-faah-token": config.mcpToken(), ...(init.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`MCP server ${init.method ?? "GET"} ${path} -> ${res.status}: ${await res.text()}`);
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("json") ? res.json() : res.text()) as Promise<T>;
}

export type Email = { sender: string; subject: string; body: string; received_via: string };

export const py = {
  createIncident: (email: Email) =>
    call<{ id: string }>("/internal/incidents", { method: "POST", body: JSON.stringify(email) }),
  listIncidents: () => call<any[]>("/internal/incidents"),
  getIncident: (id: string) => call<any>(`/internal/incidents/${encodeURIComponent(id)}`),
  markdown: (id: string) => call<string>(`/internal/incidents/${encodeURIComponent(id)}/markdown`),
  recordApproval: (id: string, body: { step_id: string; decision: "allow" | "deny"; by: string; reason?: string }) =>
    call(`/internal/incidents/${encodeURIComponent(id)}/approvals`, { method: "POST", body: JSON.stringify(body) }),
  event: (id: string, event: string, data: Record<string, unknown> = {}, status?: string) =>
    call(`/internal/incidents/${encodeURIComponent(id)}/events`, {
      method: "POST",
      body: JSON.stringify({ event, data, status }),
    }).catch((err) => console.error("[audit]", err.message)),
  cluster: () => call<Record<string, any>>("/internal/cluster"),
  resetCluster: () => call("/internal/cluster/reset", { method: "POST" }),
  health: () => fetch(`${config.mcpUrl}/health`).then((r) => r.ok).catch(() => false),
};
