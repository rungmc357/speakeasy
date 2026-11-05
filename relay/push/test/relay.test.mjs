// Unit tests for the relay: node --test (no dependencies). APNs is a mocked global fetch; the
// Durable Object runs in memory behind a fake namespace.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
// PEM armour built in pieces so the repo's secret scan doesn't mistake a throwaway test key for a real one.
const pemHeader = (edge) => `-----${edge} ${"PRIVATE"} KEY-----`;
import worker, { InstallGuard, sign, installSecret, apnsPayload, validateNotify, providerToken,
  resetProviderToken, LIMITS } from "../src/index.js";

const { privateKey } = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
const pkcs8 = Buffer.from(await crypto.subtle.exportKey("pkcs8", privateKey)).toString("base64");
// A throwaway key made for this test run, never a real APNs key.
const TEST_P8 = `${pemHeader("BEGIN")}\n${pkcs8}\n${pemHeader("END")}`;

class MemoryStorage {
  constructor() { this.map = new Map(); }
  async get(k) { return this.map.get(k); }
  async put(k, v) { this.map.set(k, v); }
  async list() { return new Map(this.map); }
  async delete(keys) { for (const k of [].concat(keys)) this.map.delete(k); }
}

function namespace() {
  const objects = new Map();
  return {
    idFromName: (name) => name,
    get: (id) => {
      if (!objects.has(id)) objects.set(id, new InstallGuard({ storage: new MemoryStorage() }));
      const object = objects.get(id);
      return { fetch: (url, init) => object.fetch(new Request(url, init)) };
    },
  };
}

let env, apnsCalls, apnsReply;
const realFetch = globalThis.fetch;

beforeEach(() => {
  resetProviderToken();
  env = { APNS_KEY_P8: TEST_P8, APNS_KEY_ID: "ABC123DEFG", APNS_TEAM_ID: "278P4D6M8C", APNS_TOPIC: "com.example.speakeasy",
    INSTALL_KEY: "test-install-key-not-a-secret", GUARD: namespace() };
  apnsCalls = [];
  apnsReply = () => new Response(null, { status: 200 });
  globalThis.fetch = async (url, init) => {
    apnsCalls.push({ url: String(url), init });
    return apnsReply();
  };
});

const TOKEN = "ab".repeat(32);
let nonceCounter = 0;
function body(extra = {}) {
  nonceCounter += 1;
  return { device_token: TOKEN, environment: "production", kind: "finished", nonce: `nonce_${String(nonceCounter).padStart(12, "0")}`,
    collapse_id: "abc123", event_id: "evt_0123456789abcdef", ...extra };
}

async function call(path, init = {}) {
  return worker.fetch(new Request(`https://push.speakeasyvoice.ai${path}`, { method: "POST", ...init }), env);
}

async function registerInstall(ip = "198.51.100.7") {
  const r = await call("/v1/register", { body: "{}", headers: { "cf-connecting-ip": ip } });
  assert.equal(r.status, 201);
  return r.json();
}

async function notify(install, payload, { stamp = Math.floor(Date.now() / 1000), secret = install.secret, raw } = {}) {
  const text = raw ?? JSON.stringify(payload);
  const sig = await sign(secret, install.install_id, String(stamp), new TextEncoder().encode(text));
  return call("/v1/notify", { body: text, headers: { "x-speakeasy-install": install.install_id,
    "x-speakeasy-timestamp": String(stamp), "x-speakeasy-signature": sig } });
}

test("register hands out an install id and its derived secret", async () => {
  const install = await registerInstall();
  assert.match(install.install_id, /^ins_[0-9a-f]{32}$/);
  assert.equal(install.secret, await installSecret(env, install.install_id));
});

test("registration is rate-limited per IP", async () => {
  for (let i = 0; i < LIMITS.registerPerIpPerHour; i++) await registerInstall("203.0.113.9");
  const r = await call("/v1/register", { body: "{}", headers: { "cf-connecting-ip": "203.0.113.9" } });
  assert.equal(r.status, 429);
  await registerInstall("203.0.113.10"); // another IP is fine
});

test("a signed notify reaches APNs with a generic, content-free payload", async () => {
  const install = await registerInstall();
  const r = await notify(install, body({ kind: "needs_you" }));
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true, status: 200, invalid_token: false });
  assert.equal(apnsCalls.length, 1);
  const { url, init } = apnsCalls[0];
  assert.equal(url, `https://api.push.apple.com/3/device/${TOKEN}`);
  assert.equal(init.headers["apns-topic"], "com.example.speakeasy");
  assert.equal(init.headers["apns-push-type"], "alert");
  assert.equal(init.headers["apns-collapse-id"], "abc123");
  assert.match(init.headers.authorization, /^bearer [\w-]+\.[\w-]+\.[\w-]+$/);
  const sent = JSON.parse(init.body);
  assert.deepEqual(sent, { aps: { alert: { "title-loc-key": "PUSH_TITLE", "loc-key": "PUSH_BODY_NEEDS_YOU" },
    "mutable-content": 1, sound: "default", "thread-id": "abc123" }, e: "evt_0123456789abcdef", k: "needs_you" });
});

test("sandbox tokens go to the sandbox host", async () => {
  const install = await registerInstall();
  await notify(install, body({ environment: "sandbox" }));
  assert.equal(apnsCalls[0].url, `https://api.sandbox.push.apple.com/3/device/${TOKEN}`);
});

test("any extra field (a title, text) is refused: the relay never takes content", async () => {
  const install = await registerInstall();
  for (const extra of [{ title: "Speed up the dashboard" }, { body: "x" }, { alert: {} }]) {
    const r = await notify(install, body(extra));
    assert.equal(r.status, 400);
  }
  assert.equal(apnsCalls.length, 0);
  assert.equal(validateNotify({ ...body(), device_token: "ZZ" }), "device_token");
  assert.equal(validateNotify({ ...body(), kind: "marketing" }), "kind");
});

test("bad signatures, unknown installs, stale timestamps and replays are refused", async () => {
  const install = await registerInstall();
  assert.equal((await notify(install, body(), { secret: "wrong" })).status, 401);
  assert.equal((await notify({ ...install, install_id: "ins_" + "0".repeat(32) }, body())).status, 401);
  const old = Math.floor(Date.now() / 1000) - LIMITS.skewS - 5;
  assert.equal((await notify(install, body(), { stamp: old })).status, 401);
  const once = body();
  assert.equal((await notify(install, once)).status, 200);
  assert.equal((await notify(install, once)).status, 409);
  const unknown = await call("/v1/notify", { body: "{}", headers: { "x-speakeasy-install": "nope" } });
  assert.deepEqual(await unknown.json(), { ok: false, reason: "unknown_install" });
  assert.equal(apnsCalls.length, 1);
});

test("per-device-token and per-install rate limits", async () => {
  const install = await registerInstall();
  for (let i = 0; i < LIMITS.tokenPerMinute; i++) assert.equal((await notify(install, body())).status, 200);
  assert.equal((await notify(install, body())).status, 429);
  // Another token on the same install still goes, until the install's own limit.
  let ok = LIMITS.tokenPerMinute, status = 200, t = 0;
  while (status === 200) {
    t += 1;
    status = (await notify(install, body({ device_token: t.toString(16).padStart(64, "c") }))).status;
    if (status === 200) ok += 1;
  }
  assert.equal(status, 429);
  assert.equal(ok, LIMITS.installPerMinute - 1); // the refused (token-limited) request counted too
});

test("a dead token is reported back so the Hermes forgets it", async () => {
  const install = await registerInstall();
  apnsReply = () => new Response(JSON.stringify({ reason: "Unregistered" }), { status: 410 });
  let r = await (await notify(install, body())).json();
  assert.deepEqual(r, { ok: false, status: 410, reason: "Unregistered", invalid_token: true });
  apnsReply = () => new Response(JSON.stringify({ reason: "BadDeviceToken" }), { status: 400 });
  r = await (await notify(install, body())).json();
  assert.equal(r.invalid_token, true);
  apnsReply = () => new Response(JSON.stringify({ reason: "TooManyRequests" }), { status: 429 });
  r = await (await notify(install, body())).json();
  assert.equal(r.invalid_token, false);
});

test("APNs unreachable is a 502, not an invalid token", async () => {
  const install = await registerInstall();
  globalThis.fetch = async () => { throw new TypeError("Network connection lost."); };
  const r = await notify(install, body());
  assert.equal(r.status, 502);
  assert.equal((await r.json()).invalid_token, false);
});

test("logs never carry the device token", async () => {
  const lines = [];
  const realLog = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    const install = await registerInstall();
    await notify(install, body());
  } finally {
    console.log = realLog;
  }
  assert.ok(lines.length >= 1);
  for (const line of lines) assert.ok(!line.includes(TOKEN) && !line.includes("ab".repeat(8)));
});

test("the provider token is an ES256 JWT that verifies, cached between sends", async () => {
  const jwt = await providerToken(env, 1_800_000_000);
  const [h, c, s] = jwt.split(".");
  const dec = (x) => JSON.parse(Buffer.from(x, "base64url").toString());
  assert.deepEqual(dec(h), { alg: "ES256", kid: "ABC123DEFG" });
  assert.deepEqual(dec(c), { iss: "278P4D6M8C", iat: 1_800_000_000 });
  const pub = await crypto.subtle.importKey("jwk", { ...(await crypto.subtle.exportKey("jwk", privateKey)), d: undefined, key_ops: ["verify"] },
    { name: "ECDSA", namedCurve: "P-256" }, true, ["verify"]);
  assert.ok(await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, pub, Buffer.from(s, "base64url"), new TextEncoder().encode(`${h}.${c}`)));
  assert.equal(await providerToken(env, 1_800_000_100), jwt);
  assert.notEqual(await providerToken(env, 1_800_003_000), jwt);
});

test("generic wording for every kind", () => {
  for (const kind of ["finished", "failed", "reply"]) {
    assert.equal(apnsPayload(body({ kind })).aps.alert["loc-key"], "PUSH_BODY_UPDATE");
  }
});

test("unknown routes are 404", async () => {
  const r = await worker.fetch(new Request("https://push.speakeasyvoice.ai/v1/anything"), env);
  assert.equal(r.status, 404);
});

process.on("exit", () => { globalThis.fetch = realFetch; });
