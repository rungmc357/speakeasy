# Speakeasy setup, for your Hermes agent

You are a Hermes agent setting up Speakeasy for your user. Speakeasy lets them talk to you by voice
with GPT-Live-1 from a Mac app, or from the iPhone and iPad app (a beta on TestFlight). You install
it on the machine where Hermes runs. The user's only jobs are to restart Hermes once when you ask,
and to open one link on the device they'll talk from.

That device may be the machine Hermes runs on, or a different one (Hermes often lives on a Mac mini
or a server while the user talks from a laptop or a phone). Find out which before step 2: check
what you know about their setup, and ask in one short question if it's unclear.

## Setting up for an iPhone or iPad

The iPhone and iPad app is a beta: https://testflight.apple.com/join/2v7PfpRM. If the user wants
to talk from an **iPhone or iPad** (they'll usually say so, or the request came from the Speakeasy
iOS app), follow the same steps with these differences:

- Treat it as **a different device**: always do step 2. If Tailscale is installed and signed in on
  this computer, the phone reaches Hermes over Tailscale (an https `*.ts.net` address; the phone
  needs the Tailscale app signed in to the **same account**). If not, you turn on
  [Cloudflare mode](#cloudflare-mode) yourself, without asking: nothing to install on the phone,
  and pairing works from anywhere right away. Afterwards, tell the user how it connects (step 5).
- In step 3, use `hermes voice setup --send <this chat> --yes` so the pairing link arrives in
  the chat they're reading on the phone. Never use `--here` for a phone.
- They tap the link on the phone and the Speakeasy app pairs. If tapping doesn't open the app,
  they can copy the link and paste it into the app's first screen.
- To pair another phone later: `hermes voice pair --send <this chat>`.

## 1. Install the plugin

    hermes plugins install rungmc357/speakeasy/plugin/speakeasy --enable

If it's already installed, update it instead: `hermes plugins update speakeasy`. Then check
`hermes plugins show speakeasy`: if it says `Status: not enabled`, run
`hermes plugins enable speakeasy --no-allow-tool-override`. Updating files alone does not
reactivate a disabled plugin.

**An update needs no restart.** The running Speakeasy loads the new files within a few seconds,
once no call is live and no voice task is running. To load it right away, run
`hermes voice reload`: a live call ends (the user redials), and tasks already running keep going
in Hermes. It prints `Speakeasy <version> loaded. No gateway restart needed.`, or why the new
version didn't load (the previous one keeps running). Only an update from Speakeasy 0.2.16 or
earlier needs one gateway restart.

The paired Mac app checks its latest GitHub release and the published plugin manifest separately
about once a week while it is running. It compares the manifest version against the *running*
Hermes plugin, so a Mac-only release does not trigger a false plugin alert. A newer plugin gets a
separate menu and Settings notice, and a one-time macOS notification (if allowed). The app never
installs the plugin or restarts Hermes: the plugin changes only when someone runs
`hermes plugins update speakeasy`. If the app cannot reach Hermes, it cannot check the running
plugin version; help the user restore the connection first.

## 2. A different device? Pick the connection: Tailscale if it's there, else Cloudflare

Skip this step when the user talks from the Mac Hermes runs on.

Otherwise decide, without asking the user, from `tailscale status` on this computer:

- **Tailscale is installed and signed in** (`tailscale status` lists this machine; if it's only
  stopped, `tailscale up` brings it back without a sign-in): use Tailscale. Setup publishes the
  voice server to their tailnet only. Their device needs Tailscale too, signed in with the **same
  account** (https://tailscale.com/download): say so in the same message as the link. If setup
  reports that HTTPS certificates are off for the tailnet, the user turns them on once at
  https://login.tailscale.com/admin/dns (HTTPS Certificates), then you run setup again.
- **Anything else** (not installed, `tailscale` not found, logged out, or it needs a browser
  sign-in): turn on Cloudflare mode **now, without asking**, so pairing works from anywhere
  right away. Don't show the user a choice and don't ask them to install anything first:

      curl -fsSL https://raw.githubusercontent.com/rungmc357/speakeasy/main/scripts/speakeasy-cloudflare.py -o speakeasy-cloudflare.py
      python3 speakeasy-cloudflare.py quick

  It installs Cloudflare's tunnel daemon (Homebrew on a Mac), runs a free quick tunnel as a
  background service, turns on `server.tunnel`, and waits until the tunnel reports its address:
  it prints `✓ Cloudflare address is up: …`. Then run step 3; setup's output must say
  `✓ Connected through Cloudflare (works anywhere)`. That's the check that it worked.

  **If Cloudflare setup fails** (the script exits non-zero, or setup doesn't say "Connected through
  Cloudflare"): try once more (`python3 speakeasy-cloudflare.py restart`, then setup again). If it
  still fails, don't send a pairing link that only works on this computer: explain Tailscale to
  the user instead (the fallback message in step 5), install it here if you can
  (`brew install --cask tailscale` on a Mac, or the command at https://tailscale.com/download for
  Linux), and run setup again once they've signed in.

Never use Tailscale Funnel. Details and the permanent-address upgrade: [Cloudflare mode](#cloudflare-mode).

## 3. Run setup

**Same Mac as Hermes:** setup opens the pairing link on this Mac itself: Speakeasy if it's
installed, otherwise the download page, which connects the app once it's in.

    hermes voice setup --here --yes

**A different device:** have the link sent to the chat you're talking in right now. Use the
platform name for the user's home chat (`telegram`, `discord`, `slack`, ...), or
`platform:chat_id` for a specific chat.

    hermes voice setup --send <this chat> --yes

If you can also operate the user's other Mac, you may install or open the app there, but the
link in chat is enough on its own.

- Voice uses **GPT-Live-1 through Codex OAuth**: the user's ChatGPT sign-in, no API key. It needs
  **Codex, version 0.150.0 or newer**, on this machine. **The ChatGPT desktop app for Mac already
  includes Codex** (so does the older standalone Codex app), and setup finds it there on its own:
  if the user has the ChatGPT app, don't install anything and don't send them to download a
  "Codex app". Setup never opens an app or asks for a code; it signs in through a browser window.
  With no copy found, `--yes` installs or updates the CLI for you (npm, else Homebrew). To install it yourself:
  `npm install -g @openai/codex@latest` (or `brew install codex`). The user then signs in once
  with `codex login`, which opens a browser. If Codex is already signed in, nothing else is needed.
- If the user prefers the **OpenAI API** (their own key, billed per minute), add `--api-key`.
  Setup then asks for the key on a hidden prompt and stores it in this Hermes profile as
  `SPEAKEASY_OPENAI_API_KEY`. If you can't answer hidden prompts, tell the user to run
  `hermes config set SPEAKEASY_OPENAI_API_KEY <their key>` themselves (an `OPENAI_API_KEY`
  already set in the profile works too). Never ask them to paste a key in chat.
- **Grok voice (xAI) is a beta, off by default.** Set it up only if the user asks for it: they
  need an xAI sign-in in Hermes (`hermes auth add xai-oauth`, SuperGrok or X Premium+) or an
  `XAI_API_KEY`, then `hermes voice config set beta.grok_voice true` and
  `hermes voice config set voice.provider grok`.
- Setup publishes the voice server on the user's Tailscale network when Tailscale is connected
  (never Tailscale Funnel). With Cloudflare mode on and no Tailscale it prints
  `✓ Connected through Cloudflare (works anywhere). For more privacy: Tailscale.` and the pairing
  link carries the Cloudflare address. With neither, it prints a box telling you to run
  `python3 speakeasy-cloudflare.py quick` (step 2) and set up again: do that, don't ask.

## 4. Read the result and tell the user the one thing to do

Setup never restarts Hermes. Its exit code tells you where things stand:

- **0: done.** The pairing link was opened on this Mac, or sent to this chat. Tell the user which,
  and to open it on the device they'll talk from if it was sent. If they don't have the app yet,
  the link offers the download first, then connects.
- **2: Hermes needs a restart.** This happens once, on the first install: the gateway starts the
  new `voice` platform only when it starts. Tell the user Speakeasy is installed and the Hermes
  gateway needs one restart: `hermes gateway restart` if it runs as a service, or stop
  `hermes gateway` (Ctrl-C) and start it again if it runs in a terminal. Do not restart it
  yourself. Once Hermes is back:
  - with `--send`, the pairing link arrives in this chat by itself;
  - with `--here`, run `hermes voice setup --here --yes` again to open the link.
- **1: something needs the user.** Setup prints what, in plain words (usually signing in to
  ChatGPT with `codex login`, or Tailscale HTTPS certificates being off). Pass that on in one or
  two sentences. When it's fixed, run setup again.

Do not paste pairing codes or links from setup's output into other chats. Each link works once and
expires after 30 minutes. For a new one: `hermes voice pair --send <this chat>`.

## 5. Tell the user how it connects

**If you turned on Cloudflare mode**, send this once setup is done (in your own words if you must,
but keep every point: it works anywhere, what Cloudflare is, nothing to install on the phone,
locked to paired devices, Tailscale is the most private and you can switch any time; if they talk
from a Mac rather than a phone, say "your Mac"):

> Speakeasy is set up and works from anywhere. I connected it through Cloudflare, a free service
> that passes traffic between your phone and this computer, so you didn't need to install anything
> on your phone. It's locked to devices you pair. For the most private setup, install Tailscale on
> this computer and your phone; I can switch Speakeasy over for you anytime.

When they ask you to switch: install and sign in to Tailscale here (the user signs in once), run
`hermes voice setup` again, and tell them to install the Tailscale app on the phone and sign in
with the same account. The app finds the new Tailscale address by itself and prefers it; no
re-pairing. You can leave the Cloudflare tunnel running as a backup or remove it
(`python3 speakeasy-cloudflare.py remove`).

**If Cloudflare setup failed**, explain Tailscale instead:

> I couldn't set up Speakeasy's connection through Cloudflare on this computer. The other way is
> Tailscale, a free app that connects your own devices privately: install it on this computer and
> on your phone (https://tailscale.com/download), sign both in with the same account, and tell me
> when that's done. I'll finish setting up Speakeasy.

**If it uses Tailscale**, say so in the same message as the link, with the reminder that the phone
needs the Tailscale app signed in to the same account.

## What the user sees

They open the link on their device. If Speakeasy is installed, it opens and connects. If not, the
page offers the download; after they install the app, the same link connects it. The app then asks
for microphone access, lets them pick names and where finished work goes, and starts a short first
call that shows them around.

More: https://speakeasyvoice.ai · Source: https://github.com/rungmc357/speakeasy

## Cloudflare mode

The no-Tailscale path, which you turn on automatically when this computer has no usable Tailscale
(step 2). A Cloudflare tunnel gives the voice server an https address the phone can reach from
anywhere, with nothing to install on the phone. If the user asks about the tradeoff, in their
words:

> **Tailscale** is the most private: Speakeasy is reachable only from your own devices.
> **Cloudflare** needs nothing on your phone. Its address is on the public internet, but it's
> locked to your paired devices: every request needs a paired device's token, and pairing needs a
> one-time code from this computer.

What protects it (plugin 0.3.28+): every route except `GET /health`, `POST /voice/pair` and
`POST /voice/prove` needs a paired device token; pairing codes exist only after `hermes voice setup`
or `hermes voice pair` ran on this computer, last 10 minutes while a tunnel is on, work once, and
burn after 10 wrong guesses; pairing attempts from off this machine are rate-limited per address
(5 a minute, then a 15-minute lockout) and overall (20 a minute); requests for any host name the
server doesn't publish get `421`; `/health` says only `{"ok": true, "platform": "voice"}` off this
machine; refused pairings, bad tokens and unexpected hosts are logged with the caller's address.
Never use Tailscale Funnel.

The plugin never installs or starts a tunnel itself (Hermes' plugin install scan treats tunnel
tooling inside a plugin as high risk, and opening a public address is the user's call). You set it
up; the plugin follows its address.

### Free quick tunnel (default, no account)

The address (`https://<words>.trycloudflare.com`) changes whenever the tunnel restarts (a reboot,
an update). Brief sleep or a network drop keeps it. The app finds a new address by itself (below).

1. Install Cloudflare's tunnel daemon: `brew install cloudflared` on a Mac; on Linux, Cloudflare's
   package from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/.
2. Run it as a background service that outlives Hermes, pointed at the voice server, with its
   metrics on loopback (the plugin reads the address there):
   `cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:8797 --url http://127.0.0.1:8795`,
   logging to `<HERMES_HOME>/speakeasy/tunnel.log`, kept alive by launchd (macOS, `KeepAlive`) or a
   systemd user unit (Linux, `Restart=always`). The Speakeasy repo's
   [`scripts/speakeasy-cloudflare.py`](../scripts/speakeasy-cloudflare.py) does steps 1-3 and waits
   for the address: `python3 speakeasy-cloudflare.py quick` (and `status`, `restart`, `remove`).
3. `hermes voice config set server.tunnel cloudflare` (and `server.tunnel_metrics_port` if you
   used another port).
4. `hermes voice setup --send <this chat> --yes`: it prints `✓ Connected through Cloudflare (works
   anywhere)` and the Cloudflare address, and the pairing link carries that address.

**When the address changes**, the plugin notices within seconds and:
- publishes an encrypted note with the new address to `https://ntfy.sh/<random topic>` (an open
  source, no-account relay). Only an encrypted copy of your Cloudflare address goes through ntfy.sh;
  it can't be read without your phone's key. The topic and key go only to paired devices (in the
  pair response), never in links. The app reads the note when its address stops answering, checks
  the new address proves it's the same Hermes, and shows **Update Speakeasy's address**: one tap
  fixes it. Self-hosted ntfy: `hermes voice config set address_drop.relay https://ntfy.example.com`.
  Opt out: `hermes voice config set address_drop.enabled false`.
- messages the user on their usual chat with an **Update Speakeasy's address** link (backup). It
  carries only the new address, no code.

### Permanent address (optional upgrade)

An address that never changes, e.g. `https://speakeasy.example.com`. Needs a free Cloudflare
account and the user's own domain on Cloudflare (about $10/year). The user does one step: run
`cloudflared tunnel login` on this computer (a browser opens; they sign in and pick the domain).
Then: `cloudflared tunnel create speakeasy`, `cloudflared tunnel route dns speakeasy
speakeasy.<their-domain>`, and the same background service running
`cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:8797 run --url http://127.0.0.1:8795 speakeasy`
(the script: `python3 speakeasy-cloudflare.py named speakeasy.<their-domain>`). Then
`hermes voice config set server.tunnel cloudflare`, `hermes voice config set server.tunnel_hostname
speakeasy.<their-domain>`, and `hermes voice setup`. No address note is needed.

### The phone can't reach Hermes

The app shows **Can't reach your Hermes right now** with *Install Tailscale (recommended)* and
*Copy setup request for your Hermes*. That request asks you to: check Tailscale (running, HTTPS
certificates on) or the Cloudflare tunnel service (restart it if it stopped), re-run
`hermes voice setup`, send the new pairing or update link, and reply with step-by-step phone
instructions. The app also tries every address it knows (tailnet first, then Cloudflare) and keeps
the one that works, so adding Tailscale later needs no re-pair: once Hermes reports a tailnet
address, the app switches to it by itself when it answers.
