// Local relay in front of the OpenAI-compatible LLM gateway.
//
// TrueForge calls models with retries disabled and undici's default 300s
// headers timeout, so a single hung gateway request kills an agent run for
// five minutes. This relay forwards each request upstream, and if response
// headers don't arrive within LLM_RELAY_HEADERS_TIMEOUT_MS (or the gateway
// answers 429/5xx) it retries, up to LLM_RELAY_ATTEMPTS times. Once headers
// arrive the (streamed) body is piped through untouched.
import "dotenv/config";
import { createServer, type IncomingMessage } from "node:http";
import { Readable } from "node:stream";

const PORT = Number(process.env.LLM_RELAY_PORT ?? 8767);
const UPSTREAM = (process.env.CUSTOM_PROVIDER_BASE_URL ?? "").replace(/\/$/, "");
const HEADERS_TIMEOUT_MS = Number(process.env.LLM_RELAY_HEADERS_TIMEOUT_MS ?? 25_000);
const NON_STREAM_TIMEOUT_MS = 120_000;
const ATTEMPTS = Number(process.env.LLM_RELAY_ATTEMPTS ?? 3);
const RETRY_STATUS = new Set([408, 429, 500, 502, 503, 504]);
const FORWARD_REQUEST_HEADERS = ["authorization", "content-type", "accept", "x-tfy-metadata"];
const DROP_RESPONSE_HEADERS = new Set(["content-encoding", "content-length", "transfer-encoding", "connection"]);

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const server = createServer(async (req, res) => {
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await readBody(req);
  const streaming = body ? /"stream"\s*:\s*true/.test(body.toString("utf8", 0, Math.min(body.length, 200_000))) : false;
  const headers: Record<string, string> = {};
  for (const name of FORWARD_REQUEST_HEADERS) {
    const v = req.headers[name];
    if (typeof v === "string") headers[name] = v;
  }

  let lastError = "";
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), streaming ? HEADERS_TIMEOUT_MS : NON_STREAM_TIMEOUT_MS);
    const started = Date.now();
    try {
      const upstream = await fetch(`${UPSTREAM}${req.url}`, {
        method: req.method,
        headers,
        body: body ? new Uint8Array(body) : undefined,
        signal: controller.signal,
      });
      if (RETRY_STATUS.has(upstream.status) && attempt < ATTEMPTS) {
        clearTimeout(timer);
        lastError = `HTTP ${upstream.status}`;
        await upstream.body?.cancel();
        console.warn(`[llm-relay] ${req.url} attempt ${attempt}: ${lastError}, retrying`);
        await sleep(500 * attempt);
        continue;
      }
      // Headers arrived: from here the stream is only bounded by the caller.
      if (streaming) clearTimeout(timer);
      const out: Record<string, string> = {};
      upstream.headers.forEach((v, k) => {
        if (!DROP_RESPONSE_HEADERS.has(k)) out[k] = v;
      });
      res.writeHead(upstream.status, out);
      if (attempt > 1) console.log(`[llm-relay] ${req.url} succeeded on attempt ${attempt} (${Date.now() - started}ms to headers)`);
      if (!upstream.body) return res.end();
      const piped = Readable.fromWeb(upstream.body as any);
      piped.on("error", (err) => {
        console.error(`[llm-relay] stream error: ${err.message}`);
        res.destroy(err);
      });
      piped.on("end", () => clearTimeout(timer));
      res.on("close", () => piped.destroy());
      piped.pipe(res);
      return;
    } catch (err: any) {
      clearTimeout(timer);
      lastError = controller.signal.aborted ? `no response headers after ${Date.now() - started}ms` : String(err?.message ?? err);
      console.warn(`[llm-relay] ${req.url} attempt ${attempt}/${ATTEMPTS}: ${lastError}`);
      if (attempt < ATTEMPTS) await sleep(500 * attempt);
    }
  }
  res.writeHead(504, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: { message: `llm-relay: upstream failed after ${ATTEMPTS} attempts: ${lastError}`, type: "upstream_timeout" } }));
});

if (!UPSTREAM) {
  console.error("CUSTOM_PROVIDER_BASE_URL is not set; the LLM relay has nothing to forward to.");
  process.exit(1);
}
server.listen(PORT, "127.0.0.1", () =>
  console.log(`LLM relay:  http://127.0.0.1:${PORT} -> ${UPSTREAM}  (headers timeout ${HEADERS_TIMEOUT_MS}ms, ${ATTEMPTS} attempts)`),
);
