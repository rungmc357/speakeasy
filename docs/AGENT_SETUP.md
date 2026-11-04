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

- Treat it as **a different device**: always do step 2. The phone reaches Hermes only over
  Tailscale (an https `*.ts.net` address), so this machine must be on Tailscale with HTTPS
  certificates on, and the phone needs the Tailscale app signed in to the **same account**.
  Tell the user that in the same message as the link.
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

## 2. A different device? Make sure Tailscale connects them

Skip this step when the user talks from the Mac Hermes runs on.

Otherwise their device reaches Hermes over Tailscale, privately, with nothing opened to the
internet. Check this machine with `tailscale status`:

- **Connected:** good. Setup publishes the voice server to their tailnet only.
- **Installed but stopped:** run `tailscale up` if you can; if it needs a browser sign-in, give
  the user the sign-in link it prints.
- **Not installed:** tell the user Speakeasy needs Tailscale when Hermes is on another computer,
  and install it if you can (`brew install --cask tailscale` on a Mac, or the command at
  https://tailscale.com/download for Linux). Signing in needs them once.

Their device needs Tailscale too, signed in with the **same account**: https://tailscale.com/download.
Say so in the same message, since you can't check their device from here (unless you can operate it).

If setup reports that HTTPS certificates are off for the tailnet, the user turns them on once at
https://login.tailscale.com/admin/dns (HTTPS Certificates), then you run setup again.

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
  the **Codex CLI, version 0.150.0 or newer**, on this machine. With `--yes`, setup installs or
  updates it for you (npm, else Homebrew). To install it yourself:
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
  (never Tailscale Funnel, so nothing is exposed to the internet).

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

## What the user sees

They open the link on their device. If Speakeasy is installed, it opens and connects. If not, the
page offers the download; after they install the app, the same link connects it. The app then asks
for microphone access, lets them pick names and where finished work goes, and starts a short first
call that shows them around.

More: https://speakeasyvoice.ai · Source: https://github.com/rungmc357/speakeasy
