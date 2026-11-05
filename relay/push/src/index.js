// Speakeasy notification relay: a content-free wake-up service between a user's own Hermes and APNs.
//
// POST /v1/register  -> 201 {install_id, secret}         (one per Hermes install; rate-limited per IP)
// POST /v1/notify    -> 200 {ok, status, reason?, invalid_token}
//   body: {device_token, environment, kind, nonce, collapse_id, event_id}   (nothing else is accepted)
//   headers: X-Speakeasy-Install, X-Speakeasy-Timestamp, X-Speakeasy-Signature
//   signature = hex HMAC-SHA256(secret, "v1\n<install>\n<timestamp>\n<sha256 hex of the raw body>")
//
// Content rule: this Worker never receives a title, a task, an answer or any words. It sends APNs a
// generic, localized alert ("Your Hermes has an update") with mutable-content and the opaque event id;
// the app's Notification Service Extension fetches the real text from the user's own Hermes.
// Logs carry statuses and short SHA-256 prefixes of tokens only.
//
// Install secrets are derived, not stored: secret = base64url(HMAC-SHA256(INSTALL_KEY, install_id)).
// State that must be exact (nonces, counters) lives in one Durable Object per install (InstallGuard).

export const KINDS = ["finished", "needs_you", "failed", "reply"];
export const ENVIRONMENTS = ["sandbox", "production"];
export const FIELDS = ["collapse_id", "device_token", "environment", "event_id", "kind", "nonce"];
export const LIMITS = {
  skewS: 300,              // a signed request is good for this long either side of now
  nonceTtlS: 900,          // nonces are remembered at least as long as a timestamp is accepted
  installPerMinute: 30,
  installPerDay: 2000,
  tokenPerMinute: 10,
  tokenPerDay: 300,
  registerPerIpPerHour: 5,
  registerPerIpPerDay: 20,
  maxBody: 1024,
};
const APNS_HOSTS = { production: "https://api.push.apple.com", sandbox: "https://api.sandbox.push.apple.com" };
// APNs reasons that mean "this token will never work again": the Hermes should forget it.
const DEAD_TOKEN_REASONS = new Set(["BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"]);

const enc = new TextEncoder();

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function sha256Hex(data) {
  return hex(await crypto.subtle.digest("SHA-256", typeof data === "string" ? enc.encode(data) : data));
}

async function hmac(keyBytes, message) {
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", key, enc.encode(message));
}

export async function installSecret(env, installId) {
  if (!env.INSTALL_KEY) throw new Error("INSTALL_KEY is not set");
  return b64url(await hmac(enc.encode(env.INSTALL_KEY), `install|${installId}`));
}

export async function sign(secret, installId, timestamp, rawBody) {
  const digest = await sha256Hex(rawBody);
  return hex(await hmac(enc.encode(secret), `v1\n${installId}\n${timestamp}\n${digest}`));
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** The notify body, or a reason it is refused. Unknown keys are refused: that is the content rule. */
export function validateNotify(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) return "body must be a JSON object";
  const keys = Object.keys(body).sort();
  if (keys.join(",") !== FIELDS.join(",")) return "exactly device_token, environment, kind, nonce, collapse_id, event_id";
  if (typeof body.device_token !== "string" || !/^[0-9a-f]{64,200}$/.test(body.device_token)) return "device_token";
  if (!ENVIRONMENTS.includes(body.environment)) return "environment";
  if (!KINDS.includes(body.kind)) return "kind";
  if (typeof body.nonce !== "string" || !/^[A-Za-z0-9_-]{16,64}$/.test(body.nonce)) return "nonce";
  if (typeof body.collapse_id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(body.collapse_id)) return "collapse_id";
  if (typeof body.event_id !== "string" || !/^[A-Za-z0-9_-]{16,64}$/.test(body.event_id)) return "event_id";
  return null;
}

/** What APNs gets: generic, localized words (from the app's Localizable.strings) and opaque ids. */
export function apnsPayload(body) {
  return {
    aps: {
      alert: { "title-loc-key": "PUSH_TITLE", "loc-key": body.kind === "needs_you" ? "PUSH_BODY_NEEDS_YOU" : "PUSH_BODY_UPDATE" },
      "mutable-content": 1,
      sound: "default",
      "thread-id": body.collapse_id,
    },
    e: body.event_id,
    k: body.kind,
  };
}

// -- APNs provider token (ES256 JWT), cached per isolate for 40 minutes ---------------------------
let cachedJwt = null; // {jwt, at, keyId}

function pemToPkcs8(pem) {
  const b64 = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

export async function providerToken(env, nowS = Math.floor(Date.now() / 1000)) {
  if (cachedJwt && cachedJwt.keyId === env.APNS_KEY_ID && nowS - cachedJwt.at < 40 * 60) return cachedJwt.jwt;
  if (!env.APNS_KEY_P8 || !env.APNS_KEY_ID || !env.APNS_TEAM_ID) throw new Error("APNs key is not configured");
  const key = await crypto.subtle.importKey("pkcs8", pemToPkcs8(env.APNS_KEY_P8),
    { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const header = b64url(enc.encode(JSON.stringify({ alg: "ES256", kid: env.APNS_KEY_ID })));
  const claims = b64url(enc.encode(JSON.stringify({ iss: env.APNS_TEAM_ID, iat: nowS })));
  // WebCrypto's ECDSA signature is already the raw r||s form JWS wants.
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, enc.encode(`${header}.${claims}`));
  cachedJwt = { jwt: `${header}.${claims}.${b64url(sig)}`, at: nowS, keyId: env.APNS_KEY_ID };
  return cachedJwt.jwt;
}

export function resetProviderToken() {
  cachedJwt = null;
}

async function sendToApns(env, body) {
  const host = env.APNS_BASE_URL || APNS_HOSTS[body.environment]; // APNS_BASE_URL: local fake only
  const jwt = await providerToken(env);
  const response = await fetch(`${host}/3/device/${body.device_token}`, {
    method: "POST",
    headers: {
      authorization: `bearer ${jwt}`,
      "apns-topic": env.APNS_TOPIC || "co.georgio.toddvoice",
      "apns-push-type": "alert",
      "apns-priority": "10",
      "apns-expiration": String(Math.floor(Date.now() / 1000) + 3600),
      "apns-collapse-id": body.collapse_id,
      "content-type": "application/json",
    },
    body: JSON.stringify(apnsPayload(body)),
  });
  let reason = "";
  if (response.status !== 200) {
    try {
      reason = String((await response.json()).reason || "");
    } catch {
      reason = "";
    }
    if (response.status === 403 && reason === "ExpiredProviderToken") resetProviderToken();
  }
  return { status: response.status, reason };
}

// -- the Durable Object: nonces + counters for one install (or one registering IP) ---------------
export class InstallGuard {
  constructor(ctx) {
    this.storage = ctx.storage;
  }

  async fetch(request) {
    const { op, nonce, token, nowS } = await request.json();
    const minute = Math.floor(nowS / 60);
    const day = Math.floor(nowS / 86400);
    const hour = Math.floor(nowS / 3600);
    const bump = async (key, limit) => {
      const n = ((await this.storage.get(key)) || 0) + 1;
      if (n > limit) return false;
      await this.storage.put(key, n);
      return true;
    };
    await this.sweep(nowS);
    if (op === "register") {
      if (!(await bump(`r:h:${hour}`, LIMITS.registerPerIpPerHour))) return json(429, { reason: "rate_limited" });
      if (!(await bump(`r:d:${day}`, LIMITS.registerPerIpPerDay))) return json(429, { reason: "rate_limited" });
      return json(200, { ok: true });
    }
    if (await this.storage.get(`n:${nonce}`)) return json(409, { reason: "replay" });
    if (!(await bump(`i:m:${minute}`, LIMITS.installPerMinute))) return json(429, { reason: "rate_limited" });
    if (!(await bump(`i:d:${day}`, LIMITS.installPerDay))) return json(429, { reason: "rate_limited" });
    if (!(await bump(`t:${token}:m:${minute}`, LIMITS.tokenPerMinute))) return json(429, { reason: "rate_limited" });
    if (!(await bump(`t:${token}:d:${day}`, LIMITS.tokenPerDay))) return json(429, { reason: "rate_limited" });
    await this.storage.put(`n:${nonce}`, nowS);
    return json(200, { ok: true });
  }

  /** Drop expired nonces and old windows (at most once a minute). */
  async sweep(nowS) {
    const last = (await this.storage.get("swept")) || 0;
    if (nowS - last < 60) return;
    await this.storage.put("swept", nowS);
    const minute = Math.floor(nowS / 60), day = Math.floor(nowS / 86400), hour = Math.floor(nowS / 3600);
    const stale = [];
    for (const [key, value] of await this.storage.list()) {
      const parts = key.split(":");
      const window = Number(parts[parts.length - 1]);
      if (key.startsWith("n:") && nowS - value > LIMITS.nonceTtlS) stale.push(key);
      else if (key.includes(":m:") && window < minute) stale.push(key);
      else if (key.includes(":h:") && window < hour) stale.push(key);
      else if (key.includes(":d:") && window < day) stale.push(key);
    }
    if (stale.length) await this.storage.delete(stale);
  }
}

async function guard(env, name, payload) {
  const stub = env.GUARD.get(env.GUARD.idFromName(name));
  return stub.fetch("https://guard/check", { method: "POST", body: JSON.stringify(payload) });
}

// -- routes ---------------------------------------------------------------------------------------
async function register(request, env, nowS) {
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const verdict = await guard(env, `ip:${await sha256Hex(ip)}`, { op: "register", nowS });
  if (verdict.status !== 200) return json(429, { reason: "rate_limited" });
  const id = new Uint8Array(16);
  crypto.getRandomValues(id);
  const installId = `ins_${hex(id)}`;
  return json(201, { install_id: installId, secret: await installSecret(env, installId) });
}

async function notify(request, env, nowS) {
  const installId = request.headers.get("x-speakeasy-install") || "";
  const stamp = request.headers.get("x-speakeasy-timestamp") || "";
  const given = request.headers.get("x-speakeasy-signature") || "";
  if (!/^ins_[0-9a-f]{32}$/.test(installId)) return json(401, { ok: false, reason: "unknown_install" });
  const blocked = (env.BLOCKED_INSTALLS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (blocked.includes(installId)) return json(403, { ok: false, reason: "blocked" });
  const raw = await request.arrayBuffer();
  if (raw.byteLength > LIMITS.maxBody) return json(413, { ok: false, reason: "too_large" });
  if (!/^\d{9,11}$/.test(stamp) || Math.abs(nowS - Number(stamp)) > LIMITS.skewS) {
    return json(401, { ok: false, reason: "stale" });
  }
  const want = await sign(await installSecret(env, installId), installId, stamp, raw);
  if (!constantTimeEqual(want, given)) return json(401, { ok: false, reason: "bad_signature" });
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json(400, { ok: false, reason: "invalid_json" });
  }
  const problem = validateNotify(body);
  if (problem) return json(400, { ok: false, reason: "invalid", field: problem });
  const tokenHash = (await sha256Hex(body.device_token)).slice(0, 16);
  const verdict = await guard(env, installId, { op: "notify", nonce: body.nonce, token: tokenHash, nowS });
  if (verdict.status !== 200) {
    const { reason } = await verdict.json();
    return json(verdict.status, { ok: false, reason });
  }
  let result;
  try {
    result = await sendToApns(env, body);
  } catch (err) {
    console.log(JSON.stringify({ at: "apns", error: String(err && err.name), token: tokenHash.slice(0, 12) }));
    return json(502, { ok: false, reason: "apns_unreachable", invalid_token: false });
  }
  const invalid = result.status === 410 || (result.status === 400 && DEAD_TOKEN_REASONS.has(result.reason));
  console.log(JSON.stringify({ at: "notify", status: result.status, reason: result.reason, token: tokenHash.slice(0, 12) }));
  return json(200, { ok: result.status === 200, status: result.status, reason: result.reason || undefined, invalid_token: invalid });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const nowS = Math.floor(Date.now() / 1000);
    try {
      if (request.method === "POST" && url.pathname === "/v1/register") return await register(request, env, nowS);
      if (request.method === "POST" && url.pathname === "/v1/notify") return await notify(request, env, nowS);
      if (request.method === "GET" && url.pathname === "/health") return json(200, { ok: true });
      return json(404, { reason: "not_found" });
    } catch (err) {
      console.log(JSON.stringify({ at: "error", error: String(err && err.name) }));
      return json(500, { ok: false, reason: "internal" });
    }
  },
};
