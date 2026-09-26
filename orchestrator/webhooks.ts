// Inbound email → incident. Mailgun (signed), SendGrid Inbound Parse (URL secret)
// and a local test route. Each returns 200 immediately; the agent runs in the background.
import { createHmac, timingSafeEqual } from "node:crypto";
import { Hono } from "hono";
import { config } from "./config.js";
import { py, type Email } from "./python.js";
import { enqueue } from "./runner.js";

function safeEqual(a: string, b: string) {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

async function accept(email: Email) {
  const incident = await py.createIncident(email);
  enqueue(incident.id);
  console.log(`[webhook] ${email.received_via}: "${email.subject}" -> ${incident.id}`);
  return { accepted: true, incident_id: incident.id };
}

export const webhooks = new Hono();

// Mailgun "store and notify"/forward route. Signature = HMAC-SHA256(key, timestamp + token).
webhooks.post("/webhooks/mailgun", async (c) => {
  if (!config.mailgunSigningKey) return c.json({ error: "MAILGUN_WEBHOOK_SIGNING_KEY not set" }, 503);
  const form = await c.req.parseBody();
  const timestamp = String(form["timestamp"] ?? "");
  const token = String(form["token"] ?? "");
  const signature = String(form["signature"] ?? "");
  const expected = createHmac("sha256", config.mailgunSigningKey).update(timestamp + token).digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(timestamp)) < 15 * 60;
  if (!signature || !safeEqual(signature, expected) || !fresh) return c.json({ error: "bad signature" }, 401);

  return c.json(await accept({
    sender: String(form["from"] ?? form["sender"] ?? ""),
    subject: String(form["subject"] ?? ""),
    body: String(form["body-plain"] ?? form["stripped-text"] ?? ""),
    received_via: "mailgun",
  }));
});

// SendGrid Inbound Parse posts multipart (from, subject, text, html). It is unsigned,
// so the Parse URL carries a shared secret: /webhooks/sendgrid?secret=...
webhooks.post("/webhooks/sendgrid", async (c) => {
  if (!config.sendgridSecret) return c.json({ error: "SENDGRID_INBOUND_SECRET not set" }, 503);
  if (!safeEqual(c.req.query("secret") ?? "", config.sendgridSecret)) return c.json({ error: "bad secret" }, 401);
  const form = await c.req.parseBody();
  return c.json(await accept({
    sender: String(form["from"] ?? ""),
    subject: String(form["subject"] ?? ""),
    body: String(form["text"] ?? form["html"] ?? ""),
    received_via: "sendgrid",
  }));
});

// Local demo route used by `npm run send` — same pipeline, JSON body, token-protected.
webhooks.post("/webhooks/test", async (c) => {
  if (!safeEqual(c.req.header("x-faah-token") ?? "", config.mcpToken())) return c.json({ error: "unauthorized" }, 401);
  const body = await c.req.json();
  return c.json(await accept({
    sender: body.sender ?? "",
    subject: body.subject ?? "",
    body: body.body ?? "",
    received_via: "test-script",
  }));
});
