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

## One-time setup (owner)

### 1. APNs auth key (Apple Developer portal)
1. Open <https://developer.apple.com/account/resources/authkeys/list> (team 278P4D6M8C).
2. Click **+** (Create a key). Key name: `Speakeasy APNs`.
3. Tick **Apple Push Notifications service (APNs)**, click **Configure**: Environment **Sandbox &
   Production**, Key restriction **Team Scoped (All Topics)**, **Save**.
4. **Continue** → **Register** → **Download** (`AuthKey_XXXXXXXXXX.p8`; it can be downloaded only
   once). Note the **Key ID** (10 characters) shown on the page.
5. In 1Password vault **Todd**, create item **Speakeasy APNs key**: attach the `.p8` (or paste its
   contents in a concealed field `p8`), field `key id`, field `team id` = `278P4D6M8C`. Delete the
   downloaded file.

### 2. Push capability + profiles (App Store Connect / portal)
- App ID `co.georgio.toddvoice`: <https://developer.apple.com/account/resources/identifiers/list> →
  `co.georgio.toddvoice` → **Capabilities** → tick **Push Notifications** → **Save** → **Confirm**.
  This invalidates the `Todd Voice App Store` profile; regenerate it under the same name (or run the
  skill script `asc_enable_capability.py PUSH_NOTIFICATIONS "Todd Voice App Store"`, which does the
  capability and the profile in one go).
- New App ID `co.georgio.toddvoice.notifications` (the Notification Service Extension; no
  capabilities needed) and an App Store profile named **Speakeasy Notifications App Store** with the
  Apple Distribution certificate. Add it to `templates/phone-export.plist` `provisioningProfiles`.
- Push needs no Apple review or approval. The archive must carry `aps-environment = production`
  (project.yml sets it per configuration), and App Store Connect will warn on upload if the profile
  lacks Push.

### 3. Deploy the Worker (Todd's Cloudflare account, `npx wrangler` already signed in)
```sh
cd relay/push
op read "op://Todd/Speakeasy APNs key/p8"     | npx wrangler secret put APNS_KEY_P8
op read "op://Todd/Speakeasy APNs key/key id" | npx wrangler secret put APNS_KEY_ID
openssl rand -base64 32                        | npx wrangler secret put INSTALL_KEY   # keep a copy in 1Password
npx wrangler deploy
curl https://push.speakeasyvoice.ai/health     # {"ok":true}
```
(`wrangler secret put` before the first deploy creates the Worker; if it refuses, deploy once first.)
Rotating `INSTALL_KEY` invalidates every install; plugins re-register by themselves on the next notify.

### 4. DNS for `push.speakeasyvoice.ai`
`speakeasyvoice.ai` is on **Porkbun** nameservers, and a Workers Custom Domain needs the zone on
Cloudflare. Pick one:
- **A (recommended):** add `speakeasyvoice.ai` to Cloudflare (free plan), copy over every Porkbun
  record (GitHub Pages A/AAAA/CNAME, the support@ MX/SPF/DKIM/DMARC records, any TXT verifications),
  switch the nameservers at Porkbun, then uncomment the `routes` line in `wrangler.toml` and
  `npx wrangler deploy` again. Cloudflare creates the `push` record itself.
- **B:** keep Porkbun and use the workers.dev address (`https://speakeasy-push.<account>.workers.dev`):
  change `DEFAULT_RELAY` in `plugin/speakeasy/push.py` and the settings default before releasing.

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
