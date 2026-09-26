# Faah.ai — on-call runbook executor

An alert email starts a TrueForge agent session. The agent grades the incident, matches it to a runbook,
investigates, drafts the complete remediation plan with a Jev risk analysis on every step, tests it in
TrueForge's Daytona sandbox, auto-applies what is provably safe and pauses for a human on everything else.
The dashboard opens itself when the plan is ready. See [PLAN.md](PLAN.md) for the design.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp[cli]" typesafe-sdk==0.7.1 pyyaml pytest httpx
npm install
cp .env.example .env        # then fill in the keys
npm run dev                 # MCP server + TrueForge + bootstrap + orchestrator
```

- Dashboard: http://localhost:3000 (click **Turn on alerts** once so pop-up notifications can show)
- TrueForge UI: http://localhost:8790 (every incident is a real TrueForge session you can open there)

Send a sample alert through the same pipeline real email uses:

```bash
npm run send                 # list the samples
npm run send -- 3 --reset    # HIGH checkout 5xx: 2 auto steps + 1 approval
```

| Sample | What it shows |
|---|---|
| 1 | LOW: batch-worker disk, fixed by rotating logs. Auto-applied only if Jev clears the LOW thresholds (real Jev currently rates it reversible at 0.57 confidence, so it asks you) |
| 2 | HIGH: search-service latency, fixed by scaling out. Auto-applied only below risk 0.20 (real Jev currently scores 0.24, so it asks you) |
| 3 | HIGH: checkout-service 5xx after a bad deploy. Scale-out, memory and rollback; the rollback is always gated by the floor |
| 4 | CRITICAL: payments-db connections saturated. Every remediation step needs your approval |
| 5 | Trick: the alert email tells the agent to `rm -rf pg_wal`. Blocked by the floor, fails the sandbox (data loss) and is refused even if someone approves it |
| 6 | No runbook matches. The agent asks you which runbook applies |

`npm run trick` runs rephrased destructive actions straight through the floor + live Jev at LOW severity
(the most permissive thresholds) for the live "try to trick it" moment.

## Jev bridge (real Jev via Vercel, or a mock)

The Python code calls Jev with the real `typesafe-sdk`, pointed at a local bridge
(`orchestrator/jev-bridge.ts`, `TYPESAFE_BASE_URL=http://127.0.0.1:8766`) that serves Jev's own API.

- `JEV_BACKEND=vercel` (default when `AI_GATEWAY_API_KEY` is set): **real Jev** (`typesafe-ai/jev`) through
  Vercel AI Gateway, using the AI SDK's `experimental_evaluate`. Needs a Vercel AI Gateway key (`vck_…`).
- `JEV_BACKEND=trueforge-judge`: a mock. 3 parallel TrueForge sessions whose probabilities are averaged;
  the dashboard labels these scores "Jev mock".
- With a direct TypeSafe key, delete `TYPESAFE_BASE_URL` and put the key in `TYPESAFE_API_KEY`.

## Model provider

`LLM_PROVIDER=custom` points TrueForge at the TrueFoundry AI gateway (`CUSTOM_PROVIDER_*`), registered as the
`gpt-model` provider. `orchestrator/llm-relay.ts` sits in front of it and retries requests that hang,
because TrueForge makes each model call only once and waits up to 300s for it.

## Real inbound email

Only the webhook port (`WEBHOOK_PORT`, default 3001) should be exposed. The dashboard binds to 127.0.0.1.

```bash
cloudflared tunnel --url http://localhost:3001     # or: ngrok http 3001
```

- **Mailgun:** Receiving → Create route → action `forward("https://<tunnel>/webhooks/mailgun")`.
  Set `MAILGUN_WEBHOOK_SIGNING_KEY` (Sending → Webhooks → HTTP webhook signing key). Requests are HMAC-verified.
- **SendGrid:** Settings → Inbound Parse → URL `https://<tunnel>/webhooks/sendgrid?secret=<SENDGRID_INBOUND_SECRET>`.

## Layout

| Path | What |
|---|---|
| `orchestrator/` | TypeScript. TrueForge SDK: bootstrap, sessions, turn streaming, approvals; webhooks; dashboard API + SSE |
| `src/` | Python MCP server (the tools TrueForge calls): parser, runbook matcher, severity, floor, Jev, router, planner, toy cluster |
| `runbooks/`, `config/services.yaml` | Runbook catalog with triggers; service tiers |
| `public/` | Dashboard |
| `samples/` | Demo alert emails |
| `data/` | Incident state, audit logs (JSONL), toy cluster state. Delete the folder to start fresh |

## Tests

```bash
npm test     # pytest (fake Jev) + TypeScript typecheck
```
