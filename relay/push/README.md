# Speakeasy notification relay (`relay/push`)

A Cloudflare Worker that wakes a user's iPhone through APNs when their own Hermes asks, **without
ever seeing what the notification is about**. The words travel only between the user's Hermes and
their phone (the app's Notification Service Extension fetches them over the paired connection).

```
Hermes plugin (push.py) ──HMAC-signed {device_token, environment, kind, nonce, collapse_id, event_id}──▶ this Worker
this Worker ──ES256 JWT + {"aps": {"alert": {"title-loc-key": "PUSH_TITLE", "loc-key": "PUSH_BODY_UPDATE"},
                            "mutable-content": 1, "thread-id": collapse_id}, "e": event_id, "k": kind}──▶ APNs ──▶ iPhone
iPhone NSE ──GET /voice/push/events/{event_id} (device token)──▶ user's Hermes ──▶ "Done: Speed up the dashboard"
```

- **Content rule**: `/v1/notify` refuses any field beyond the six above (400). The alert is localized
  generic text from the app ("Speakeasy" / "Your Hermes has an update" / "Your Hermes needs you"),
  shown as is when the phone can't reach Hermes in time.
- **Abuse protection**: each Hermes install registers once (`POST /v1/register`, 5/hour and 20/day
  per IP) and gets `install_id` + `secret` (derived: `HMAC(INSTALL_KEY, install_id)`, nothing stored).
  Every notify is signed (`X-Speakeasy-Signature` = hex HMAC-SHA256(secret,
  `"v1\n<install>\n<timestamp>\n<sha256 hex of body>"`)), timestamps must be within 5 minutes,
  nonces are single-use, and one Durable Object per install enforces 30/min + 2000/day per install
  and 10/min + 300/day per device token. `BLOCKED_INSTALLS` (var) refuses an install outright.
- **Dead tokens**: APNs 410, or 400 `BadDeviceToken` / `DeviceTokenNotForTopic` / `ExpiredToken`,
  comes back as `invalid_token: true`; the plugin then forgets that token.
- **Logs**: one JSON line per notify with the APNs status and a 12-hex-char SHA-256 prefix of the
  token. Never the token, never the install secret.
- **Environments**: the phone reports `sandbox` (Debug/Xcode builds) or `production`
  (TestFlight/App Store); the Worker picks `api.sandbox.push.apple.com` or `api.push.apple.com`.

## Tests

```sh
npm test                     # node --test, no dependencies: mocked APNs fetch, in-memory Durable Object
node test/e2e.mjs            # relay on :8787 + fake APNs on :8790 (records to $E2E_OUT)
# then, from plugin/: E2E_OUT=<same file> python tests/e2e_push.py   (real plugin → relay → fake APNs → read back)
```

## Deployment (live)

- **URL**: `https://speakeasy-push.todd-562.workers.dev` (Todd's Cloudflare account, free plan,
  workers.dev). It is the plugin's default (`DEFAULT_RELAY` in `plugin/speakeasy/push.py`, the
  `push.relay_url` settings default); any https relay can be set in settings instead.
- **APNs key**: Key ID `ZBP98Z8JTQ`, team `278P4D6M8C`, Sandbox & Production, team scoped. The `.p8` is
  the 1Password document **Speakeasy APNs key** (vault Todd). The install key is the 1Password item
  **Speakeasy push relay install key**.
- **Apple**: Push Notifications is on the iPhone app's App ID (profile `Todd Voice App Store`
  regenerated with it); the extension's App ID (`<app bundle id>.notifications`) has the profile
  `Speakeasy Notifications App Store` (no capabilities). Push needs no Apple review.

Redeploy / rotate (from `relay/push`, never printing a secret):
```sh
npx wrangler deploy --var APNS_TOPIC:<app bundle id>   # first deploy; keep_vars keeps it after
op document get "Speakeasy APNs key" --vault Todd | npx wrangler secret put APNS_KEY_P8
printf %s ZBP98Z8JTQ                               | npx wrangler secret put APNS_KEY_ID
op item get "Speakeasy push relay install key" --vault Todd --fields password --reveal | npx wrangler secret put INSTALL_KEY
curl https://speakeasy-push.todd-562.workers.dev/health      # {"ok":true}
```
(`APNS_TEAM_ID` is a plain var in `wrangler.toml`; `APNS_TOPIC`, the app's bundle id, is set once with `--var` and kept by `keep_vars`.) Rotating `INSTALL_KEY`
invalidates every install; plugins re-register by themselves on the next notify.

### Later: `push.speakeasyvoice.ai`
`speakeasyvoice.ai` is on **Porkbun** nameservers, and a Workers Custom Domain needs the zone on
Cloudflare. To move: add the zone to Cloudflare (free), copy every Porkbun record (GitHub Pages
A/AAAA/CNAME, support@ MX/SPF/DKIM/DMARC, TXT verifications), switch nameservers, uncomment `routes`
in `wrangler.toml`, deploy, then change the plugin default. Keep workers.dev enabled so older plugins
keep working.

## Privacy page (proposed wording, not published)

> **Notifications.** If you turn on notifications, your iPhone gives your own Hermes a notification
> address from Apple. When a job finishes, needs you, fails, or gets a reply while Speakeasy is
> closed, your Hermes asks Speakeasy's notification relay (run on Cloudflare) to wake your phone.
> The relay receives only that address, the kind of event (for example "finished") and random
> identifiers; it never receives the name of a job, its result or any other words, and it keeps
> no record beyond short-lived counters used to prevent abuse. Apple delivers a generic "Your
> Hermes has an update"; your phone then fetches the actual text directly from your Hermes over
> your own connection, and shows the generic text if it can't reach it. Turning notifications off
> in Speakeasy or in iOS Settings stops them, and your Hermes forgets the address.

## Cost (Cloudflare Workers Free)
- Workers: 100,000 requests/day, 10 ms CPU per request (a notify uses well under 1 ms of CPU; the
  APNs JWT is cached for 40 minutes per isolate). Waiting on APNs is wall time, not CPU.
- Durable Objects (SQLite-backed, free plan): 100,000 requests/day, 13,000 GB-s/day, 100,000 rows
  written/day, 5 GB. A notify writes about 6 rows, so the binding limit is roughly **15,000
  notifications/day** across all users, $0. Workers Paid ($5/month) lifts that to millions.
- APNs itself is free.
