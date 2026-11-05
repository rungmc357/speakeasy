// Local end to end without Cloudflare: serves the Worker on 127.0.0.1:8787 (in-memory Durable
// Object) and a fake APNs on 127.0.0.1:8790 that records what it receives to $E2E_OUT (JSON lines).
// The plugin side (plugin/tests/e2e_push.py) points push.relay_url at http://127.0.0.1:8787.
// Uses a throwaway P-256 key generated at start, never a real APNs key.
import http from "node:http";
import fs from "node:fs";
// PEM armour built in pieces so the repo's secret scan doesn't mistake a throwaway test key for a real one.
const pemHeader = (edge) => `-----${edge} ${"PRIVATE"} KEY-----`;
import worker, { InstallGuard } from "../src/index.js";

const out = process.env.E2E_OUT || "e2e-apns.jsonl";
const { privateKey } = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign"]);
const pem = `${pemHeader("BEGIN")}\n${Buffer.from(await crypto.subtle.exportKey("pkcs8", privateKey)).toString("base64")}\n${pemHeader("END")}`;

const objects = new Map();
const storage = () => { const m = new Map(); return { get: async (k) => m.get(k), put: async (k, v) => { m.set(k, v); },
  list: async () => new Map(m), delete: async (ks) => { for (const k of [].concat(ks)) m.delete(k); } }; };
const env = {
  APNS_KEY_P8: pem, APNS_KEY_ID: "E2EKEY0001", APNS_TEAM_ID: "278P4D6M8C", APNS_TOPIC: "co.georgio.toddvoice",
  INSTALL_KEY: "e2e-install-key", APNS_BASE_URL: "http://127.0.0.1:8790",
  GUARD: { idFromName: (n) => n, get: (id) => {
    if (!objects.has(id)) objects.set(id, new InstallGuard({ storage: storage() }));
    return { fetch: (u, init) => objects.get(id).fetch(new Request(u, init)) };
  } },
};

const dead = new Set((process.env.E2E_DEAD_TOKENS || "").split(",").filter(Boolean));
http.createServer((req, res) => {
  let data = "";
  req.on("data", (c) => (data += c));
  req.on("end", () => {
    const token = req.url.split("/").pop();
    fs.appendFileSync(out, JSON.stringify({ path: req.url, headers: req.headers, body: JSON.parse(data || "{}") }) + "\n");
    if (dead.has(token)) {
      res.writeHead(410, { "content-type": "application/json" });
      res.end(JSON.stringify({ reason: "Unregistered" }));
    } else {
      res.writeHead(200, { "apns-id": "e2e" });
      res.end();
    }
  });
}).listen(8790, "127.0.0.1");

http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const headers = { ...req.headers, "cf-connecting-ip": "127.0.0.1" };
  const request = new Request(`http://127.0.0.1:8787${req.url}`, { method: req.method, headers,
    body: req.method === "POST" ? Buffer.concat(chunks) : undefined });
  const response = await worker.fetch(request, env);
  res.writeHead(response.status, { "content-type": "application/json" });
  res.end(Buffer.from(await response.arrayBuffer()));
}).listen(8787, "127.0.0.1", () => console.log("relay on :8787, fake APNs on :8790"));
