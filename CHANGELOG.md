# What's new in Speakeasy

Speakeasy has two parts that update separately: the **Mac app** (updates through Check for Updates) and the **Hermes plugin** (updates when you run `hermes plugins update speakeasy`; the running plugin then loads the new version without a gateway restart). Each entry says which one it came with.

## Unreleased — Home's state for build 73 (plugin only, apps not yet using it)

- **Home knows what's current.** Home's thread now marks the current exchange: your latest message, Todd's reply and the status line of the job it started. Send something new and the previous exchange becomes transcript only; the transcript itself stays complete and in order. *(plugin)*
- **Home resets after 20 minutes.** With no message from either side for 20 minutes, Home reports itself clean. The reset never removes anything waiting in the widget stack. *(plugin)*
- **A widget stack.** A separate list of finished results and things that need a decision (a question, an approval, a job that stopped), never running work. Each says which job or chat it came from and which message holds its result. Dismissed on Home, it stays listed for Work; dismissed on Work, it's gone for good. Answering or replying to one takes it off; work that starts is a normal job, and its result comes back as a new item. *(plugin)*
- **Recommendations, no model call.** Up to three suggestions from unfinished business (an unsent draft, a skipped question, a stopped job you dismissed, anything dismissed on Home but still open) and follow-ups on jobs that just finished. Each names its source, a dismissed one never comes back, and when nothing personal fits there are three fixed generic ideas, marked as generic. *(plugin)*
- Older apps see the same Home as before: all of this is new fields and new routes. *(plugin)*

## Unreleased — email drafts you can see and change

- **The draft shows up by itself.** Ask for an email and, when the job finishes, the email itself arrives as a new message at the bottom of Home's chat, without a second "show me". The job's card where you asked says done; tap it to jump down to the email. *(plugin + iPhone build 73 + Mac)*
- **Every recipient, with their address.** The card shows To, Cc and Bcc with names and full addresses, and "Reply in thread: … · 4 messages" when it answers a thread. If the job couldn't find a person you named, the card says so, with Add. A company you name next to someone ("email Ben and his firm") is never a missing recipient: it shows beside their name when their address is at that firm. *(plugin + iPhone build 73)*
- **Tap for the whole email.** All headers, the full body, the message it replies to, attachments. Change recipients, subject or body right there, then Send, Save draft or Discard. Send waits 5 seconds with Undo. *(plugin + iPhone build 73)*
- **Nothing you said gets dropped.** Every person, company and detail you name goes into the draft, or the card says why not. *(plugin)*

## Plugin 0.3.49 — Home's model chip, waiting jobs and replies

- **The model chip is what runs.** The chip above Home's composer now always names the model your jobs actually run on: your pick on the chip, else the job model saved in Speakeasy's settings, else your Hermes default. Before, it could show your Hermes default while jobs ran on the saved one. *(plugin)*
- **A job waiting its turn shows up.** When a new job would edit the same project as one already running (say, two changes to the same app), it waits, and now you can see it: its card says "Waiting · after Checkout fix" right away, and Home tells you ("I'll start that as soon as the paint search wraps up."). *(plugin; the waiting card shows on iPhone with the next build)*
- **Start now anyway.** Tap a waiting card to start it right away, beside the other job. *(plugin + next iPhone build)*
- **Questions never wait.** Questions, lookups, proposals, plans and research start at once, even while another job is busy on the same project. *(plugin)*
- **Reply to a message or a card.** Swipe left on anything in Home to reply to it. Replying to a job's card ("make it 8 pm instead") goes to that job. *(plugin + iPhone build 72)*
- **Files opens on the first try.** The Files page no longer says it can't reach your Hermes on its first load; the first look through your files is much faster. *(plugin + iPhone build 72)*

## iPhone build 72 — Home, tidied

- **One row on top.** The side panel button, the running-work pill (centred) and settings share one line. There's no Clear button: Home is a chat, so old messages scroll away; dismiss a stuck card from its long-press menu.
- **Pull down the pill.** A tap or short pull shows your jobs as slim rows; pull further for all your work (Needs you, Running, Scheduled, Done). Push up to close.
- **Call turns into Send.** Start typing and the call button becomes Send in the same spot (the mic steps aside too); clear the words and both come back. On iPad and the opened iPhone Duo, Call sits in the right-hand rail with the side panel and settings.
- **Chat bubbles and replies.** Home's messages are bubbles; swipe left on one to reply, or on a card to reply to that job.
- **Sending feels instant.** Your photo or clip leaves the composer as you tap Send and shows in the thread while it uploads; if it can't send, the message says so with Retry.
- **No more flash of the old screen** when the app opens.
- **Waiting jobs.** A job queued behind another shows "Waiting · after …" with Start now anyway.
- **Pictures** on before/after cards open full screen; pinch to zoom, drag down to close.
### Also in 0.3.49: updates never cost you a job

- **Queued jobs survive an update or restart.** A job waiting behind another job on the same project used to turn into a stopped card when the plugin updated or Hermes restarted. It now stays queued and starts on its own when the other job finishes, or right away if that one is already done. *(plugin)*
- **Updates load while jobs run.** A plugin update no longer waits for every running job to finish; it loads as soon as no call is on, and running jobs carry on through it with their progress and answer intact. *(plugin)*
- **Sending a job that has to wait no longer hangs** for 20 seconds before its card shows. *(plugin)*
- **Small talk in a new chat looks like chat.** "Hey" in a new conversation is named "New chat" until you ask for something real (no more "Handle … task #24"), and the reply is marked as plain chat so the apps can drop the task header around it. *(plugin; the apps follow in the next build)*
- **Bursts of connections are queued, not dropped.** When the app loads many things at once (a Files page of thumbnails, a carousel), the server no longer turns some of them away with "Couldn't reach your Hermes". *(plugin)*
- **Quieter Hermes log.** A repeating background re-import is logged once instead of every 2 seconds, so the log keeps a full day of history. *(plugin)*

## Plugin 0.3.48 — Home is chattier

- **Home talks like a person.** Instead of a clipped "On it." or "Checking.", Home answers with a natural line about what you asked ("Let me take a look at where the release is at."), never the same line twice in a row. Small talk gets a real reply, and answers about your jobs lead with the result, then a sentence on what it means. *(plugin)*
- **Longer video clips.** Home now takes clips up to 25 MB once the app has compressed them (up to 2 minutes); PDFs and text files stay at 10 MB. *(plugin; the next iPhone build compresses clips before sending)*

## Plugin 0.3.47 · iPhone build 71 — Home is a chat

- **Home talks back.** What you type on Home now goes to your assistant first, and it answers right there in the thread. Saying hi, a quick question, or "thanks" gets a reply, not a task card. *(plugin; needs iPhone build 71 or the next Mac app to show the replies)*
- **Real work still runs as a job.** When a message needs doing ("find a quiet cafe near the river"), you get a one-line reply and the job's card right under it, which updates in place until the answer lands. *(plugin)*
- **Follow-ups go to the right job.** "Will I need an umbrella?" is answered from the forecast job you just ran; "actually, Portland, Oregon" corrects the running search; "cancel that" stops it. *(plugin)*
- **Two asks, two jobs.** "Find a hardware store open Sunday and look up how long to boil an egg" starts two jobs, each with its own card. *(plugin)*
- **Pick the model your jobs run on.** The chip above the Home composer chooses the model Home's jobs use (your Hermes default unless you change it), each family shown once at its newest version with the rest under More…. Chat replies stay quick on Auto: the newest Sonnet, GPT Terra or Haiku you already have connected in Hermes, read from your list (it never test-calls your models or changes your settings). With none of those connected, the chat uses your work model. Home's chat shows no model name on each message. *(plugin + iPhone build 71)*
- **Answers come as cards.** A finished job leads with a card made for it (a forecast, a list with a winner, a pull request's checks, a status board, a document preview), with up to two buttons for the next step, under a reply that starts with the verdict. *(plugin + iPhone build 71)*
- **Send a video clip to Home.** It shows in the thread as its poster with a play mark and its length, and the job sees it. *(plugin + iPhone build 71)*
- **Running jobs say what they are doing** in one live line ("Reading the reviews"), never a placeholder or a spinner, and a canceled job's card stays in the thread saying Canceled. *(plugin + iPhone build 71)*
- **Conversations fixes.** The list no longer sticks on Archived when you switch views, a call shows once in the Live Activity, and running jobs are easy to find. *(iPhone build 71)*
- **Voice memos to Home** get the same reply-or-job treatment, transcribed by your Hermes. *(plugin)*
- **Other chats stay out of Home's thread.** A running Discord or Telegram conversation shows in the Work strip, and only joins the thread when it needs you. *(plugin)*

- **"Later" on the call card works.** Putting the card from an ended call off now takes it off Home until its time (or until it changes), and Undo brings it straight back, the same as any other card. *(plugin)*
- **A dismissed call card comes back when it has more to say.** If a task from the call finishes a few seconds after you hang up, the card returns with that answer instead of staying hidden. *(plugin)*
- **Answering a call card is refused cleanly** instead of failing, and no task is started by it. *(plugin)*

## Plugin 0.3.46 — Swipe to dismiss works

- **Swiping a Home card away works again.** On iPhone build 69 every swipe said "That didn't go through." The plugin now accepts the card ids exactly as the apps send them, including the call card, a group of stopped jobs, and answers in the "Earlier today" row. *(plugin)*
## Plugin 0.3.37 (unreleased) — Task cards say what the task is really doing

- **Bookings come from your calendar first.** Asked what you are booked for or where you are staying, the task now checks your calendar and confirmations before notes, never says "only one" from a partial source, and gives dates and cities. The voice says it is checking until the task finishes, and replies no longer start with a stray comma or stacked "good catch / thanks" filler.
- **A live line instead of a plan.** A running task's card now says what it has actually done in the last couple of minutes ("Editing store.py and router.py", "Running the tests after editing calls.py"), with a short "So far" recap underneath, written from its real steps (and worded by the side model when it's available). The plan guessed at handoff is no longer shown as progress, and no card shows step numbers.
- **Honest waits.** One long step says what it's waiting on and for how long ("Waiting on the tests · 4 min"); a task that has gone quiet says so ("No new steps · 3 min"); the handoff line stops posing as "now" after a minute with nothing reported.
- **No spoken "Sent."** A message or picture you type to a task during a call is delivered silently; the voice only speaks about it if you ask.
- **One ask, one task.** Pictures sent during a call no longer fail with "couldn't reach Hermes" when Hermes is just slow to read them in (it's asked again for the same task, with more time), and something you add seconds after a task starts goes into that task instead of starting a twin that answers a second time.
- **No choice card for a closing offer.** "Want me to hold, or walk you through it?" at the end of an answer is just talk now, not a card and a "Decide on…" task in Done.
- **Titles name the subject.** Titles made only of generic words ("Check work status", "Decide on holding") or pointing at "the earlier task" are rejected and written again.

## Plugin 0.3.45 · Mac 0.3.23 · iPhone build 69 — A tidier Home

- **No counting headline.** Home no longer opens with a sentence counting your cards; the cards say it. An empty Home says "All caught up." *(apps)*
- **Swipe to dismiss.** On iPhone and iPad, swipe a Home card left to move it to Earlier, with Undo in the card's place. It moves on every device and is never deleted. *(iPhone build 69)*
- **Ending a call returns to Home,** with the call's card and its answers there, instead of staying on the call's transcript. *(iPhone build 69)*
- **Clear.** A single tap sends all done answers to Earlier, read or unread (on iPhone and iPad it sits in the top bar, with Undo). *(apps)*
- **The call card says what the call did,** in the plugin's own words. *(apps)*
- **No empty picture tiles:** a picture shows only once it has loaded. *(iPhone build 69)*
- **Plainer failure lines:** "Your AI account is out of credits." instead of naming the account after your Hermes. *(Plugin)*
- **A stopped card says what stopped and why,** with the one next step ("Photo move stopped: the AI model declined this one. Retry or dismiss."), never a bare "It stopped.". With no reason at all it says "Stopped without finishing" and the task's name. *(Plugin)*
- **A retry replaces its stopped card:** once a later run in the same conversation succeeds (or is running), the stopped card goes. Several jobs stopped by one cause are one card ("3 jobs stopped: …"). *(Plugin)*
- **One card per conversation:** repeated answers in one conversation (a call's follow-ups, a run of lamp commands) fold into the newest. *(Plugin)*
- **Earlier today · N:** answers more than 4 hours old leave the cards for one row (sent as `earlier.today`; build 68 ignores it, builds 69/70 draw it). *(Plugin)*
- **A call's answers stay on Home** under the call card after you hang up (heard is read, not gone); opening one in the app lets it leave. The call card opens the conversation the call's task ran in (never a session the app can't find), says what was found in real sentences ("You leave Sat 8:15 AM from Pier 11."), and lists facts the voice said with no task behind them. *(Plugin)*

## Plugin 0.3.44 — Plain words on Home's live line

- **One card per piece of work.** A conversation and the task it started show as one Home card: the task's card, with that conversation as its origin line.
- **Answers you heard on a call keep their own line** on Home instead of "Told you on the call."
- **No raw commands on Home.** A working card's live line is always plain words: what the task last said it's doing, or "Working…", never a command or a tool's name.

## Plugin 0.3.43 — Home shows only what's live or new

- **Old answers no longer flood Home.** An answer leaves Home once you've opened it, at 4 AM, or 12 hours after it finished, whichever comes first, whether or not you opened it (it stays in Earlier). Answers you heard on a call or got in a chat thread count as read. After an upgrade, older answers start out as seen.
- **"From yesterday" means yesterday.** Older cards say the weekday or the date.
- **Whole titles on Home,** never a phrase cut in the middle.
- **No empty picture tiles:** a picture whose file is gone isn't sent.
- **The call card says what the call did:** what it started, answered and asked, or "Short call, nothing started."
- **Dismiss and Clear sync:** a dismissed card moves to Earlier on every device, with Undo. *(needs iPhone build 69 to swipe)*

## Plugin 0.3.42 · Mac 0.3.22 · iPhone build 68 — A new Home screen, and calmer, clearer pages

The plugin works with older apps too. Items marked *(apps)* need Mac 0.3.22 or iPhone build 68.

**New**

- **Home.** One screen for everything: what's live and what's new, in one ordered list, with anything that needs you at the top. A call happens right there on Home instead of on a separate screen, and the call's buttons sit in one bar along the bottom; tap the keyboard button to type a message, and tap it again or send to put it away. On iPad, Home sits beside the open conversation. On the Mac, the menu bar opens Home, the main window shows it between the sidebar and the conversation, and a small floating bar holds the call's controls. The widgets read the same list. *(apps)*
- **A calm first run.** iPhone, iPad and Mac open with four short screens about what Speakeasy does, then setup. "See how it works" plays a short demo; no sample call is needed. *(apps)*
- **Who hears what.** A plain privacy screen in setup (Mac: right after the voice; iPhone and iPad: from the You're connected screen) and in Settings (top of Voice, and About). It says what stays on your computer (your agent, sessions, files and tools) and what the call voice hears (with the ChatGPT voice, OpenAI hears the call itself and a short note about you and your running tasks' names, but not your sessions, files, tools or attachments). It follows your voice setting, and adds a line about Cloudflare, notifications or listening mode only when you use them. *(apps)*
- **Code you can copy.** Every code block in an answer shows as code with a one-tap Copy that copies exactly what's in the block. *(apps)*
- **Email drafts and pictures on the Mac's chat page.** Review a draft there (Send, Deny or Revise) and look at the pictures a task sent, as on the iPhone. *(apps)*
- **Usage numbers are opt-in.** Tokens, context and cost on task cards and chat pages are off until you turn them on in Settings, per device. *(apps)*
- **One word for unfinished work.** Everything that stopped early now says "Didn't finish", on every screen and in notifications. Pairing and save problems read in plain words, with no error codes, and network trouble no longer tells you to restart Hermes.
- **Every message lands where you sent it.** Each typed message carries its own id from the app to Hermes and back, so its bubble, its Sent / Steered / Queued receipt and its row on the session page always match, even when two messages say the same thing; a message held for a task that stops is marked as not sent instead of hanging. *(apps)*

**Pages and cards** *(apps)*

- **Session pages say each thing once.** The bar says where the chat lives and that it's working; the task card says what it's doing now; the task's start time stands in for the day line under it. Your own messages have one look, notes and tool steps one pill style, and the page header sits on its own band so messages no longer scroll up into the title.
- **"Didn't finish" says why,** right under it, so its buttons never stand alone.
- **"Needs you" looks the same everywhere,** with one brass mark.
- **Question cards have one close button, inside the card.** On the home card it replaces the separate "Not needed" link; on a call the pinned card's close button no longer hangs off the corner.
- **Chart cards say what is plotted and in which order,** and the bars follow it; values are readable and an old reading says so.
- **Easier to read.** Secondary text, recessed surfaces and every colour pairing now meet contrast checks in light and dark.
- **Top bars match:** three button styles, and a conversation's title gets more room.

**Calls**

- **The call buttons stay whole on screen** and match the message box, on every iPhone and iPad. *(apps)*
- **"Thinking…" and "Speaking" no longer draw on top of each other** while the state changes. *(apps)*
- **Landscape with the keyboard open:** the work tray no longer covers the message box and call controls. *(apps)*
- **Pinned weather on iPad** shows its whole hourly row. *(apps)*
- **A message typed just as a paused call resumes** stays in the transcript.
- **A call that drops while paused stays paused,** instead of ending as "Didn't finish".
- **A spoken correction right after typing** is heard, not dropped as an echo of the typed words.
- **A finished task's answer, question and pictures are saved before it says "done",** so they are always there when you open it.

**Setup**

- **You're connected** shows your computer's name rather than an address, and makes Sign in the main button while the voice isn't signed in. *(apps)*
- **No Tailscale?** Setup's note now talks to you when you run it yourself, and no longer points at a file you don't have.
- **Install docs** match the real first run, including Hermes 0.22.0 or newer.

**Fixes**

- Session pages no longer show Speakeasy's own task notes inside your words.
- Swiping a card away on the board has a steady reveal and a readable delete colour. *(apps)*
- Test and reliability fixes across the plugin and apps.

## Plugin 0.3.41 — Commands and code from a finished task show in the conversation

- **The written answer shows, not just the spoken line.** When a task finishes, its conversation page now shows the full answer, with commands and code blocks intact, so the line the voice told you to paste is really there. The conversation list keeps the short line as its preview.
- **"On screen" means on screen.** The voice only says a command is on screen when it is; a command the voice had but the written answer didn't is added to the answer.

## Plugin 0.3.40 · Mac 0.3.21 · iPhone build 67 — Voice notes, agents, skills, files, and calls that keep the whole conversation

The plugin works with older apps too. Items marked *(apps)* need Mac 0.3.21 or iPhone build 67.

**New**

- **Voice messages are the recording itself.** Hold the mic on a chat's page and the recording is what goes, like a voice note in Telegram or Discord: it plays back in the chat with its length and waveform, Hermes writes down what you said under it, and a failed one keeps a Retry that sends exactly the same note again. *(apps)*
- **Pick which agent a new chat talks to.** If you run several Hermes profiles, allow them in the Mac's Settings › Agents and New chat gets a small agent picker ("Ask Research desk anything"). The chat stays with that agent, on its own model, and shows its name in the list. *(apps)*
- **Type / for your skills.** A `/` at the start of a message box lists your Hermes skills; pick one, add what you want ("/soup-scaler for twelve people") and it runs that skill, the same as `/skill` in Telegram. *(apps)*
- **Files your agent made.** A new Files row lists every file Hermes sent you in a reply, from every chat, with Open chat to jump back. *(apps)*
- **See your approval rules.** Settings › Approvals shows, in plain words, how your Hermes handles risky commands. Read-only for now. *(apps)*
- **Model, context and usage.** Task cards and chat pages say which model did the work and how full its context got ("Claude Opus 5.5 · about 22% of context used"), and each chat shows an estimated cost or "Included in your plan". *(apps)*

**Calls**

- **A task gets the whole call.** Work started on a call now sees the whole conversation in whole turns, plus a short list of the call's other tasks, so "do the first one you mentioned" finds what it points at.
- **Corrections stick.** "No, the guest room" replaces the misheard word in the task's name, its brief and what the voice says next.
- **Follow-ups are just your new words.** A later message to a running task sends what you added, not the whole set of instructions again.
- **Honest updates.** The voice keeps a task's tense ("submitting" is not "submitted"), doesn't claim to be "picking up where we left off", and doesn't read task titles aloud. "Where are you at?" is answered from the task's latest status; "what is it testing?" is asked of the task itself when the status can't say.
- **A picture and a note are one message.** Attach a photo on a call, type "the labels run off the edge", and the agent gets both together and acts on them, without reading your note back to you.
- **Dropped calls say so plainly.** "The call dropped. Your tasks keep running." with Call again, instead of billing words.
- **Big cards fit the call screen.** A tall weather or list card no longer pushes the orb, captions and buttons off the screen. *(apps)*

**Sending to a running task** *(apps)*

- **Choose after Send.** The permanent Steer / When it's done row is gone. Sending to a running task opens a small pop-up at the Send button: Steer now or Queue for after. Nothing goes until you pick, and nothing is remembered.

**Clearer messages**

- **No terminal commands or program errors.** Failure reasons, Settings › Voice and setup say what to do in plain words ("Add key", "Sign in to ChatGPT", "Ask your Hermes to sign in to X") instead of telling you to run a command or read a log, and error names like "TimeoutError" never reach the screen.
- **Setup checks Hermes has an AI model.** A brand-new Hermes with no model says so ("Hermes has no AI model yet") instead of letting your first task fail.
- **Banners clear themselves.** "Can't load your work right now" and "Couldn't reach your Hermes" go away as soon as things load again.
- **Questions stay settled.** A question you answered, skipped or set aside closes everywhere at once and doesn't come back.

**Fixes**

- The iPhone Duo's outer screen no longer freezes when you tap a chat in the side panel. *(apps)*
- On the Lock Screen and in widgets, a private task's details stay hidden until the phone is unlocked. *(apps)*
- Largest text sizes: side panel labels, Approve and Deny, and the message box no longer get cut off. *(apps)*
- iPad: a half-width window opens the conversation list beside the chat, and setup says iPad, not iPhone. *(apps)*
- VoiceOver names every button on chat, call and task screens; small call-screen buttons are easier to hit. *(apps)*
- The keyboard no longer covers the box you're typing in. *(apps)*
- A count shows as "4 of 9 builds", never as a progress bar. A chat's page shows one "On call" timer, not two. *(apps)*
- The Kanban board shows when it scrolls; scheduled jobs show your time zone in words. *(Mac)*
- Mac setup is shorter: connect, sign in to a voice if needed, then one "You're ready to talk" screen. *(Mac)*
- Chat pages no longer show Hermes' internal wrappers in your own messages; the secure key box doesn't pop up for a skill that already has a way in; `hermes voice pair` hands out a link that works on the phone; setup only says voice is on when it is.

## Plugin 0.3.39 — Fewer false "Refused by the model's safety filter" failures

Works with the current Mac and iPhone apps; no app update needed.

- **Tasks aren't refused for how they're briefed.** Some tasks failed with "Refused by the model's safety filter" because the instructions for the live status line used wording the model's filter read as a request for its private reasoning. The live status now asks only for the step being done and what it found, so it keeps working as before without tripping the filter.
- **A refused turn is tried once more.** If the filter still refuses a turn, Speakeasy quietly sends it again in the same conversation before showing the task as failed, and your answer to a task's question still reaches it.
- **No "Your request was not processed" replies.** That internal line no longer shows as the reply or the preview; a reply sent again after a failure shows once.

## Plugin 0.3.38 — Messages on a chat's page answer that chat

Works with the current Mac and iPhone apps; no app update needed.

- **A message on a chat's page goes to that chat.** On a Discord or Telegram conversation you had continued in Speakeasy before, a new message could be answered by the older Speakeasy follow-up instead, replying to its own earlier question. Now a follow-up only takes the message while it is the newest part of the conversation; once the chat has moved on, the reply starts from the chat's latest messages.
- **Typed messages are labelled typed.** A message typed on a session page with no call no longer shows as "From a call", including ones already in your history.
- **Tool text isn't shown as a reply.** Comments the agent posts to a tracker from the shell stay steps of the work in chats instead of appearing as its answer to you.

## Plugin 0.3.37 · Mac 0.3.20 · iPhone build 66 — Steer or queue, honest task cards, clean session chats

The plugin works with older apps too. Items marked *(apps)* need Mac 0.3.20 or iPhone build 66. The apps also read every session as a messaging thread: bubbles, day dividers and times, finished tasks as one quiet line after the reply they belong to, and the Mac opens every task in its session page.

**Talking to a running task**
- **Steer or queue.** A message to a task that's still working goes in right away (steer, the default) or waits and is sent as its next turn when it's done (queue). Held messages survive a reload, can be cancelled or sent now, and a steered message shows whether the task has read it yet ("Seen", "Waiting for the current step to finish"). *(apps: the Steer now / When it's done choice)*
- **One ask, one task.** Something you add seconds after a task starts joins that task instead of starting a twin, including an alternative ("or using the menu bar"), a change ("set it to 75") or pushback; only an ask about something clearly different starts its own. Pictures sent during a call no longer fail with "couldn't reach Hermes" when Hermes is just slow to read them.
- **"Adding that to…" once.** The voice says where an addition went once per task, and not at all when it can only have gone one place. A message you type to a task during a call is delivered silently.
- **Retry keeps your photos.** Retrying a failed task (or typing "try again") sends the same pictures and files again.

**Task cards and spoken updates**
- **A live line instead of a plan.** A running task says what it actually did in the last couple of minutes ("Editing store.py and router.py") with a short "So far" recap, written from its real steps. Guessed plans and step counts are gone; a long wait says what it's waiting on ("Waiting on the tests · 4 min") and a quiet task says so.
- **Spoken updates say what the card says**, only when the card changed, backing off when nothing is new, and never the same line twice. An answer you already opened in the app is never read out afterwards.
- **Titles and headlines name the subject.** Generic titles ("Check work status") are rewritten, a finished task's headline is the outcome ("The export works again"), not how the reply opened, and every task says what you asked.
- **The model shown is the one that ran.** The model chip and a task's model name what your Hermes actually served, not just what the config file says.
- **A safety-filter refusal says so.** When the model's safety filter refuses a request, the task says that and suggests trying again or switching model, instead of blaming a firewall. *(apps: Try again and Switch model buttons)*
- **Closing offers aren't choices.** "Want me to hold, or walk you through it?" at the end of an answer is just talk, not a decision card.

**Calls with lots going on**
- Long and many tasks no longer slow down Stop, follow-ups or spoken updates on a call.
- A quiet call with a task still running isn't paused for being idle, and a call at its length limit with work running is kept on for a while, then paused so you can resume it, never ended as an error.

**Session pages and the conversation list**
- **Only what people said.** Hermes' notes to itself stay out of session pages, previews, titles and search: background and helper notices, system notes, skill files (a /skill command reads "/name what you asked"), compaction summaries (one "Earlier messages were summarized" divider), Discord and Telegram routing headers, reply wrappers, model/usage footers, split markers and "[SILENT]" replies. A reply keeps a small "Replying to …" quote.
- **Voice messages transcribed by your Hermes**, like a voice note in Telegram: the whole clip, with the speech-to-text you set up; the recording is deleted once transcribed and neither audio nor words are logged. *(apps)*
- **The list shows when a chat last moved**, and a chat you continued from Speakeasy previews the reply it got.
- **Board workers and scheduled jobs out of the way.** Kanban board workers and scheduled runs can be hidden from the list, never show as unread, and a worker's chat opens on a compact card header instead of its long instructions. *(apps: the switch)*
- **Board.** Your Hermes Kanban boards as Ready, Running, Blocked and Done columns, read-only. *(apps)*
- **Decision cards that don't hang around.** A question the conversation moved past stops waiting on you and stays as a quiet "Question · moved on" line; cards can be dismissed everywhere or answered in your own words. *(apps: ✕ and Reply…)*
- **Setup check.** Starting a call before setup is finished lists what's ready and what isn't, each with one fix (sign in, turn voice on, copy /restart). *(apps)*
- Works on Hermes 0.23 (which ships without PyYAML).

## Plugin 0.3.36 — Easier setup, and session pages end with the latest message

- **Session pages end with the latest message.** When you've continued a Discord or Telegram chat from Speakeasy, those replies now sit in the chat in time order instead of piling up at the bottom after newer messages.
- **Cleaner messages in history.** Behind-the-scenes text Hermes adds to chat messages (message ids, sender tags, memory notes, report labels like DONE: and SPOKEN:) no longer shows on session pages, list previews or search results; background-job notices show as one short line.
- **Restart by sending /restart.** When setup needs Hermes to restart, it tells you to send /restart in your chat with Hermes; the terminal command is only the fallback. A gateway that never ran is started, not restarted.
- **Setup works when Tailscale doesn't.** If Tailscale is stopped or its HTTPS certificates are off, setup switches to Cloudflare with a plain note instead of sending a link that only works on this computer, and Cloudflare mode no longer needs Homebrew.
- **Pairing links say how long they last** (30 minutes, or 10 in Cloudflare mode), and setup can show the pairing QR code right in the terminal.
- **Two Hermes profiles on one computer.** Setup moves off a port another profile already uses, never pairs with another profile's server, and names the profile it set up.
- Clearer setup wording for iPhone, iPad or Mac, a plain "update Hermes" message on a Hermes that's too old, and the Grok fix names the right sign-in command.

## Plugin 0.3.35 — Lost-track jobs settle, better job names and steps

- **A job that lost its connection mid-run no longer sits "Stopped" forever.** If the live link to a job drops before it reports back, Speakeasy now picks up its final answer from the job's session once it goes quiet (or marks it interrupted if none arrives) and the answer shows up as usual. Such a job can also be dismissed right away. On iPhone and Mac the x and swipe work on it once the apps update.
- **Jobs get a real name.** A quick follow-up like "any luck?" no longer names the whole job ("Check the earlier task"); the job takes the name of the first request in it that says what the work is.
- **Steps name the actual work.** A job's progress steps now say what they're about ("Find why the checkout test fails") instead of stock lines like "Find the cause" and "Fix and test". A plan made only of stock lines isn't shown.

## Plugin 0.3.34 · Mac 0.3.19 · iPhone build 65 — Your whole Hermes, in one app

- **Pick the model for each chat.** A chip above the message field on a session page or New chat shows which model and reasoning level the next message runs on; tap it to use another one for this chat, make it your default, or go back to the default. Set Speakeasy's default in Settings › Jobs. On a call, "use the smart model for this" (or "the fast model") picks for that job. The list is your Hermes' own (signed-in providers only), and nothing about where your words go changes.
- **Scheduled.** Your Hermes scheduled jobs in the side panel on iPhone, iPad and Mac: what's coming up, paused or done, each in plain English ("Weekdays at 8:00 AM"), with its last result and recent runs (open a run to read its conversation). Run now, Pause/Resume, Edit or Delete, or make a new one by typing or saying it ("every weekday at 8 summarize my inbox"): you see a preview and nothing is saved until you tap Save.
- **Type secrets into a secure box, not the chat.** When a job needs an API key, your Mac password for one `sudo` command, or a website login or code, the app shows a secure box for it. What you type goes straight to your Hermes for that one step and never reaches the model, the chat or the logs.
- **Share to Speakeasy on iPhone and iPad.** Send links, text, photos and files from any app's share sheet as a new chat or into a recent session, with a note or a voice message.
- **Side panel gestures on iPhone and iPad.** The panel opens from its button (the left edge is the system back swipe again), closing it never taps what's underneath, and sessions open over the board with Home to go back. The call stays a tap away on every page.
- **Source filters only show sources you have.** The source menu lists Speakeasy, Discord, Telegram and the rest only when there are sessions from them, and hides itself when everything comes from one place.
- **Replies always continue the same session.** Writing to a Discord, Telegram or other linked chat keeps going in one Speakeasy session instead of starting a new one each time, and a New chat becomes its session as soon as you send.
- **Answered questions clear everywhere.** Answer a question or approval on one device and it disappears from the others, the Lock Screen and widgets within a couple of seconds.
- **Press and hold a session** (right-click on the Mac) for Rename, Pin, Archive, Mark as read, Copy link, and Stop while it's running.
- **Swipe a session to pin or archive it**, with Undo.
- **Archived filter.** Archived sessions have their own filter, and search still finds them (marked archived) so you can open or unarchive them.
- **Live task cards on session pages.** See what a task is doing now and the step it's on, the tool steps it took (in plain words) and the helpers it brought in, right on the session page.
- **Faster history.** The panel and session pages load quicker and update live.
- **Clearer connection messages.** A slow reply no longer blames Tailscale when its connection is fine, and a brief hiccup while your Hermes updates no longer shows as "can't reach your Hermes".
- The voice no longer turns requests down itself: anything that needs work goes to your Hermes, which decides under your own rules.
- **Sign in to ChatGPT from the app:** Speakeasy finds an existing Codex sign-in first, and otherwise shows a code you approve on your phone.
- **Setup can sign Codex in remotely** and send the link to the chat you're installing from.

## Plugin 0.3.33 · Mac 0.3.18 · iPhone build 64 — Speakeasy as your Hermes app

- **Every session and task in a side panel** on iPhone, iPad and Mac, newest first, with search. Open any session (Speakeasy, Hermes API, Discord, Telegram, terminal) to read it, tool calls folded away.
- **Write to any session** by typing or with a voice message (transcribed on your device). **New chat** starts a fresh job.
- **Discord and Telegram chats continue as a linked Speakeasy task.** Your message and the chat's recent context go to a Speakeasy task; the reply shows in Speakeasy and is never posted back to the chat.
- **Attach photos, screenshots, camera shots and files** (PDF, text) from iPhone and iPad with the **+** next to the field: on a session page, a new chat, a job, or the call. Hermes looks at them the same way it does when you share on the Mac. Big photos are resized and sent as JPEG; location and camera details are removed on the phone and again on your Hermes. Up to 3 per message; pictures up to 2.5 MB after resizing, files up to 10 MB. Anything you attach and don't send is deleted after 30 minutes.
- **History window on the Mac** (⌘Y).
- **Faster spoken updates on calls**, thanks to [@pinkhoodie](https://github.com/pinkhoodie): with your mic open, a long task's first "here's where it's at" comes 12-20 seconds in, and it keeps checking in instead of going quiet. **How often is up to you:** Settings › Spoken updates, every 30 s, 45 s (the default), 1 min, 1.5 min or 2 min (`hermes voice config set speech.interval_seconds 60`). Finished work, questions and approvals are always spoken, even when you're muted.
- **Your jobs stay put when a call pauses or ends.** A new call starts with every job that's still running, waiting on you, or finished and not cleared. They stay on the board until you clear them, and the assistant knows about them, so "how's the Kyoto one going?" or "add Osaka to that" goes to the right job. A quiet call that paused itself says so: "Paused after 15 min of quiet · your jobs keep running", with Resume. **Pause a quiet call** is now Never, 5, 10, 15, 30 or 60 min, and the default is now 15 min instead of 5 (if yours was on 5, it moves to 15 once; set it back anytime). **End a call after** adds **No limit** (`hermes voice config set voice.max_call_minutes 0`). Keep in mind that a call you forget about keeps using your voice provider's minutes. Both settings are on iPhone, iPad and Mac.
- **Light, Dark or System.** iPhone, iPad and Mac now have a light look (warm paper, ink text, deeper brass) next to the dark one. Choose in Settings › Appearance on the phone or Settings › General › Appearance on the Mac; it's kept on that device and System is the default.
- **Swiping a job away works.** On iPhone and iPad a left swipe on a board card shows Dismiss (a long swipe dismisses at once) instead of opening the job, and Dismiss takes the card off right away with an **Undo**. Dismissed jobs stay gone after a refresh or a new call.
- **Better "Try saying" ideas.** Every example works as the first thing you say on a call (no more "Actually, make it shorter"), and home examples (lights, heat, fans) only show when home control is on for your Hermes.
- **Start a call from anywhere, set up in a minute.** The board's tip has **Set it up**: step-by-step for the Action Button, Control Center, the Lock Screen and Siri/Shortcuts, plus Open Shortcuts. The tip hides by itself once you've started a call from outside the app, ✕ hides it for good, and Settings › Start a call from anywhere brings it back.
- **Your phone tells you when a job is done, needs you, fails, or gets a reply**, even with Speakeasy closed. Speakeasy asks for permission the first time a job outlives your call, not at launch.
- **The words never leave your Hermes on the way.** Your Hermes sends only a wake-up signal through Speakeasy's notification relay; your phone fetches the text straight from your Hermes and shows "Done: …" / "Needs you: …".
- **Pick which ones** in Settings › Notifications: Finished, Needs you, Failed, New replies.

## Plugin 0.3.32 · iPhone build 63 — Works from anywhere, right after setup

- **No Tailscale? Setup connects through Cloudflare by itself.** Your agent checks for Tailscale; if it isn't there, it turns on Cloudflare mode without asking, so your phone works from anywhere straight away. It then tells you what it did, and that Tailscale is the most private option.
- **The phone says so once:** a one-time "Connected through Cloudflare" note with a Set up Tailscale button.
- **Adding Tailscale later switches over by itself.** As soon as your Hermes has a Tailscale address, the phone moves to it (never mid-call). No re-pairing.

## Plugin 0.3.31 · Mac 0.3.16 · iPhone build 62 — See the helpers a job brings in

- **Helpers on the job page.** When a job hands parts of the work to helpers, the job page lists each one with what it's doing now, how long it's been going, and how it ended. Open a helper to see what it was asked, what it found and every step it took.
- A helper that fails never fails the job, and nothing about where your words go changes.

## Plugin 0.3.30 · Mac 0.3.15 · iPhone build 61 — Settings on your phone, and tidier sessions

- **Settings on iPhone and iPad.** Change your voice, model, names, spoken updates, call limits, where results go and more, right from the phone. Changes save to your Hermes, so every device sees them. Connection details, pair again and unpair live there too.
- **Lots more "Try saying" ideas** on the start screen, rotating a few at a time.
- **Hermes sessions behind voice jobs get real names** ("Find why the checkout test fails") instead of "Handle voice task from Speakeasy #3".
- **Each device reports its app version**, so `hermes voice devices` shows which build every phone and Mac is on.
- Fix: nothing a call is waiting on can hold up Hermes when it shuts down.

## Plugin 0.3.29 · Mac 0.3.14 · iPhone build 60 — Speakeasy away from home without Tailscale, and typing on a call

### Away from home, without Tailscale

- **Cloudflare mode.** No Tailscale? Your agent can give Speakeasy a Cloudflare address: nothing to install on your phone. The address is public but locked to your paired devices. Tailscale stays the recommended, most private way, and setup says so. *(Plugin; the tunnel is set up by your agent, see docs/AGENT_SETUP.md › Cloudflare mode)*
- **The app follows a changed address.** A free Cloudflare address changes when the tunnel restarts. The plugin leaves an encrypted note with the new one (only your phone can read it), and the iPhone app shows **Update Speakeasy's address**: one tap fixes it. Hermes also messages you a link. *(Plugin; needs the iPhone app update)*
- **Hardened for the public internet:** every request needs a paired device; pairing codes come only from this computer, last 10 minutes and are rate-limited with a lockout; unknown host names are refused; `/health` says nothing beyond "up" off this machine. *(Plugin)*
- **The phone keeps every address** of your Hermes (tailnet and Cloudflare) and uses whichever works, so adding Tailscale later needs no re-pair. When none works, a calm card offers Install Tailscale and a setup request to copy for your Hermes. *(iPhone app)*

### Type to your assistant on a call

- **Type instead of talking, right on the call.** On iPhone the call screen has a **Message <assistant name>** box (when your plugin supports it); the Mac panel shows typed turns from any device on the call. What you type counts exactly like saying it: the voice answers it, it goes into the call's transcript as your turn, and anything it asks for is handed off the same way speech is. It never goes to a job just because that job is on screen. Thanks to @pinkhoodie ([#12](https://github.com/rungmc357/speakeasy/pull/12), [#13](https://github.com/rungmc357/speakeasy/pull/13), [apple #5](https://github.com/rungmc357/speakeasy-apple/pull/5)). *(Plugin, iPhone; Mac shows them)*
- **One box per screen, labelled with who gets it.** On the call screen it's **Message <assistant name>**; on a job page it's **Message this job**, which goes straight to that job; on a decision, **Something else…** answers that decision. On iPhone the call's box steps aside while a job page is open. *(Apps)*
- **Your typed turns show once.** A typed turn, or a message to a job, appears once in the transcript on every device on the call, marked as typed (with the job's name when it went to a job). *(Apps)*

### Widgets and Live Activity

- **Redesigned to match the app:** the warm dark look with the orb, job names in full, and what each job is doing now instead of step counts. Decisions show the question with clear buttons; Reply is labelled.

### Thanks to @pinkhoodie

[@pinkhoodie](https://github.com/pinkhoodie) built much of Speakeasy. Features of his already shipped:

- Thanks to @pinkhoodie for **saying why a task failed** (out of credits, rejected key, rate limit, missing model) ([#1](https://github.com/rungmc357/speakeasy/pull/1), Mac 0.2.16 / plugin 0.2.39).
- Thanks to @pinkhoodie for **Look at this**: share your screen, a picture or a file with a spoken request ([#3](https://github.com/rungmc357/speakeasy/pull/3), Mac 0.3.5 / plugin 0.3.13).
- Thanks to @pinkhoodie for **listening mode during a call**: pause the call, hear the room, pick up knowing what was said ([#4](https://github.com/rungmc357/speakeasy/pull/4), Mac 0.2.18 / plugin 0.2.51).
- Thanks to @pinkhoodie for **voice status updates that narrate thinking and background work** ([#5](https://github.com/rungmc357/speakeasy/pull/5), plugin 0.3.12).
- Thanks to @pinkhoodie for **renaming past threads' stand-in titles with Apple Intelligence** ([#6](https://github.com/rungmc357/speakeasy/pull/6), Mac 0.3.6).
- Thanks to @pinkhoodie for **ducking the Mac's volume while the voice speaks** ([#7](https://github.com/rungmc357/speakeasy/pull/7), Mac 0.3.6).
- Thanks to @pinkhoodie for **text replies**: full written answers in the expanded chat view, voice unchanged ([#8](https://github.com/rungmc357/speakeasy/pull/8), plugin 0.3.14 / Mac 0.3.6).
- Thanks to @pinkhoodie for **the Vision Pro spatial command center**: tasks, cards and live data in 3D (speakeasy-apple #2).
- Thanks to @pinkhoodie for **pairing that explains failures, and the Test connection button** (speakeasy-apple #1).
- Thanks to @pinkhoodie for **⌘↩ approve / ⌘⌫ deny on the approval row** (speakeasy-apple #3).


## Plugin 0.3.27 — Names that fit, and a board that stays cleared

- **Follow-ups are named after the task they answer**, never an unrelated earlier task or the chat thread's name. Names never include your name or the assistant's, and obvious mishearings are fixed ("a good sleeper… a premium speaker" becomes "Find a premium speaker for the office").
- **Cleared tasks stay cleared**, including ones with an email draft waiting.
- Apps: no more "Work unavailable: cancelled" when the board refreshes, and old tasks no longer come back after you clear the list.

## Plugin 0.3.26 — Calls that let you finish, and real job names

- **It lets you finish.** Results, questions and updates wait for a real pause; a thought that trails off isn't sent until you say so ("Want me to send that?").
- **"Wait", "I wasn't done", "don't add that" are conversation, not jobs.** The voice stops and listens, or takes back what it just sent.
- **"What's it doing?" gets a real answer** from the job: what you asked, its latest tool calls, what it found, or what it's queued behind.
- **Specific task names**, with follow-ups named after the job they belong to.
- **Job pages show their own work**: a queued job says so, bookkeeping never counts as progress, and generic plans are dropped.

## Plugin 0.3.24 — Setup finds Codex inside the ChatGPT app

- **No second Codex install for ChatGPT users.** Voice runs on the copy of Codex that comes with the ChatGPT desktop app. Setup already looked there; it now also checks the older standalone Codex app, which can be left behind after OpenAI folded it into ChatGPT. The setup guide your Hermes agent reads now says the ChatGPT app is enough, so the agent won't install Codex again or send you off to download a "Codex app". If no copy is found, the message tells you to install the ChatGPT app or the Codex CLI. *(Plugin)*

## Plugin 0.3.23 — Calls that keep you posted, and one-tap plugin updates

- **Calls don't go quiet while work runs.** About every 45 seconds of silence while a task runs, the call says one short line about what it's doing, written from its actual tool calls ("I've gone through the checkout logs and I'm rerunning the test now"), even when the agent writes no progress notes. With nothing new it says what's running and for how long, and after three of those it waits 90 seconds. Never while you're muted, talking, or have a question or approval waiting; answers are always spoken, muted or not. Off with the same "spoken progress" setting. *(Plugin; mute needs Mac with this change)*
- **One-tap plugin update.** When a newer plugin is out, the apps show it with an **Update** button; it starts an update job on your Hermes like any other job, no call needed (`POST /voice/work`, capability `start_jobs`). *(Plugin; needs the app update)*
- **"Update my Speakeasy plugin" on a call.** The voice hands it off and says it switches over when you hang up; the job runs `hermes plugins update speakeasy` and never forces a reload, so the call isn't cut off. *(Plugin)*
- **A plain `hermes plugins update speakeasy` passes Hermes' install scan,** with no `--force`. *(Plugin)*
- Jobs started with no call (typed follow-ups too) now hold back an automatic plugin reload until they finish, like jobs started on a call. *(Plugin)*

## Plugin 0.3.22 — Keys and sign-ins only through Hermes

- **Keys and sign-ins are read only through Hermes.** API keys, the Hermes API key and the xAI sign-in now come from Hermes' own secret, env and credential-pool readers, for the right profile; Speakeasy no longer opens the `.env` or `auth.json` files itself, and setup saves values through Hermes' env writer. Everything you already set keeps working, unchanged. *(Plugin)*
- **Codex sign-in is checked with `codex login status` alone.** Speakeasy no longer looks at Codex's credential file. After a call proves the sign-in no longer works, Settings says "not signed in" for two minutes, then trusts Codex again. *(Plugin)*
- **`OPENAI_API_KEY` works for the API-key voice.** `SPEAKEASY_OPENAI_API_KEY` is still the documented name and wins when both are set. *(Plugin)*
- **Clearer setup.** The restart message names the exact commands and says it's a first-install step only; the setup guide covers installing Codex (0.150.0 or newer), the iPhone and iPad beta, and Grok voice (beta, off by default). *(Plugin, docs)*
- Declares the oldest Hermes it supports (`requires_hermes` in plugin.yaml). *(Plugin)*

## Mac 0.3.12 — One-click plugin updates, and a calmer job page

- **Update the plugin from the app.** When a newer Speakeasy plugin is out, the board shows it with an **Update** button. It starts the update as a job on your Hermes, and the app confirms when the new version is running. *(Mac)*
- **Mute reaches your Hermes.** The plugin now knows when you're muted, so the "still working" updates wait while you are; answers are still spoken. *(Mac)*
- **No step-count graphic.** A job without a picture of its own goes straight to what it found and the tool calls it's making. *(Mac)*

## Mac 0.3.11 — Watch the work, and type to any job

- **See what a job is doing while it works.** The job page shows the latest tool call live, with every earlier call one click away (what it was given, what came back, how long it took), plus what it has found so far and the files, pages and commands it touched. *(Mac, with plugin 0.3.20+)*
- **A picture of the work.** Test reruns show a pass/fail grid, research shows its sources, code changes show their size, and plans show each step with how long it took. *(Mac)*
- **Type to a job.** Every job has a reply field: it steers a running job, answers one that's waiting, or starts a follow-up on a finished one. Decisions take "Something else…" in your own words. *(Mac, with plugin 0.3.20+)*
- **A calmer board.** A greeting with what needs you, new answers with the answer showing, running jobs as live tiles, and one Stop button per job. *(Mac)*

## Plugin 0.3.21 — Live activity for every kind of job

- **Jobs that work inside a Discord or Telegram thread show their steps too.** A follow-up typed to a job that lives in a chat thread, and jobs started in a new thread, now show each tool call live (name, what it was given, how long it took), like every other job. *(Plugin)*
- **A typed follow-up opens.** The job a typed follow-up starts can always be opened from the reply, even when it ends up running in a chat thread. *(Plugin)*
- **Finished promptly, with the right answer.** A thread job is marked done within about a second of its reply, and is never settled with that thread's previous reply. *(Plugin)*

## Plugin 0.3.20 — Type to a job, and see what it's doing while it works

- **Reply to a job in writing.** Typing in a job's reply bar sends it straight to that job: added to it while it runs, the answer when it's waiting on you, or a follow-up that picks up where it left off once it's done. During a call, the voice knows but doesn't read it back. *(Plugin; the reply bar appears once the apps update)*
- **"Something else…" on decisions.** Answer a decision card in your own words instead of picking an option. *(Plugin; needs the app update)*
- **It tells you where your follow-up went.** When something you say is added to a job already going, the voice says "Adding that to *the job*." Say "no, the dashboard one" right after and it moves there instead of starting a new job. *(Plugin)*
- Where your spoken words go is otherwise unchanged. *(Plugin)*

- **Live activity on the job page.** While a job runs you see what it's doing right now, in plain words: "Read checkout.spec.ts", "Ran the tests", "Searched the web for…". Each step shows whether it's still going, done or failed. *(Plugin; shown by the apps once they update)*
- **What it has found so far.** Things the job says it has learned along the way appear as short findings, before the final answer. *(Plugin; shown by the apps once they update)*
- **What it touched.** The files, pages and commands a job worked with, listed once each, with lines added and removed for edited files when known. *(Plugin; shown by the apps once they update)*
- **A picture of the work when there is one.** A job rerunning a test shows how many runs passed, a code change shows its size, and research shows the sources it read. *(Plugin; shown by the apps once they update)*
- **Private by design.** Passwords, keys, tokens and full folder paths never appear in the activity. It's kept with the job, so reopening the app shows the same feed.

## Plugin 0.3.18 — Pre-launch fixes

- **Card buttons do what they say.** "Turn on" always turns a light on, even if the card was out of date or tapped on two devices at once.
- **No stuck choice cards.** If the connection drops mid-call, the call's option cards go away instead of waiting for you forever, and an old card can't start a job by accident.
- **Safer pairing.** Ten wrong pairing-code guesses retire the code; ask your Hermes for a new one.
- **Grok calls on iPhone come back after a phone call or Siri** instead of going silent.
- **Saving settings no longer shows a false "can't reach Hermes."**

## Mac 0.3.10 — See what the voice is doing, and act on what it finds

- **Know when it's listening, hearing you, thinking or talking.** The orb is bigger, moves with your voice and its voice, and there's a new "Thinking" state between you finishing and the reply starting. The line being said shows as a live caption. *(Mac)*
- **Decisions you can weigh.** Options are numbered, Compare puts them side by side with risk, undo and time, and you get 5 seconds to undo a pick. The buttons stay in reach however long the brief is. *(Mac)*
- **Approvals with the full picture.** Each approval says what will happen, to what and why, with real Approve and Deny buttons. *(Mac)*
- **Progress with numbers.** Jobs show which step they're on, how long they've run and, when there's enough to go on, about how long is left. Failed jobs say why and have a Retry button. *(Mac, with plugin 0.3.17)*
- **Cards you can use.** Lights have on/off and brightness buttons, charts show values as you move over them, places open in Maps, and a short note confirms what happened. *(Mac, with plugin 0.3.17)*
- **Lighter job pages.** A job opens with the answer, its cards and a few key points; the long text sits behind Details and the steps fold down to the current one. *(Mac)*
- **Microphone problems come with a fix.** If the mic is blocked or stops working, the panel says so plainly with an Open Settings or Restart call button. *(Mac)*

## Plugin 0.3.17 — Clearer task cards, buttons on cards, and no more "On it" with nothing started

- **"On it" always means something started.** If the voice tells you it's on something, a task now really exists for it. If what you said was too short to act on, the voice says plainly that nothing started and asks what you want. *(Plugin)*
- **Corrections replace, they don't pile up.** Saying "guest room, sorry" right after "turn on the living room lights" fixes the request instead of starting a second one, and saying the same thing twice starts one task. *(Plugin)*
- **Task names read like to-dos.** Titles no longer stop mid-word ("Turn off the office lamp, turn"), skip your slips ("be- er, sorry"), and a home command is named for what changed, like *Office lamp off · Bedroom lamps on*. A quick "leave it" or "ship it" takes the name of the task it answers instead of a blank. *(Plugin)*
- **Failed tasks say why.** Every failed task carries one plain sentence about what went wrong, such as "It timed out waiting on Hermes's model provider", never just "unknown". *(Plugin; shown by the apps once they update)*
- **Progress you can read.** Tasks now report when they started and finished, which step they're on ("Step 3 of 5"), and, once there's enough to go on, about how long is left. *(Plugin; shown by the apps once they update)*
- **Buttons on cards.** A home command's card has an on/off button for each light, switch or fan it touched, plus brightness for lights, so you can undo or adjust without calling again. *(Plugin; buttons appear once the apps update)*
- **Pick from the screen.** When the voice offers you a choice out loud ("the quick summary or the full report?"), the options also appear as a card you can tap. *(Plugin; tappable once the apps update)*
- **Key points first.** Long answers with sections now come with up to four short key points, so the apps can show those and tuck the full text behind Details. *(Plugin)*
- **Speakeasy comes back by itself after a failed update.** If loading a new version and falling back to the old one both failed (for example, a brief disk error), the voice server used to stay down until Hermes restarted. It now keeps retrying, every few seconds at first, and `hermes voice reload` retries right away. *(Plugin)*

## Plugin 0.3.16 — Your assistant answers to its own name

- **No more "Hey, I'm ChatGPT."** On a GPT-Live call, the voice could open by introducing itself as ChatGPT instead of the name you gave your assistant. The call instructions now end by restating its name, so a "hey, what's up" gets your assistant, not the voice provider. *(Plugin)*

## Mac 0.3.9 — Know what each new voice is

- **The new voices say where they're from.** Each GPT-Live 1.1 voice in Settings › Voice now names the language it was made for (French, German, Hindi, Japanese, Korean, Mexican Spanish, Brazilian Portuguese or English), followed by a short note on how it sounds. On iPhone, the new voices also get their play buttons. *(Mac app and iPhone)*

## Mac 0.3.8 and Plugin 0.3.15 — Talk to Grok, and no more "Status unconfirmed"

- **Grok as your voice (beta).** Speakeasy can now use xAI's Grok realtime voice instead of GPT-Live. Open Settings › Voice, turn on **Grok Voice (beta)**, then pick Grok as the voice provider (voices Ara, Rex, Sal, Eve and Leo). It uses the X / SuperGrok sign-in your Hermes already has, so no API key is needed. If Hermes isn't signed in to xAI, Settings tells you how. Tasks, cards and approvals work the same as with the other voices. *(Plugin and Mac app)*
- **Long steps no longer look stuck.** A task working in a chat thread used to show *Status unconfirmed* whenever one step ran longer than about a minute and a half. Speakeasy now asks Hermes whether the task is really still running, keeps the card live while it is, and shows what it's in the middle of when it has been quiet for a while. A task that has actually died still shows as stale. *(Plugin)*

## Mac 0.3.7 — Hear the new voices

- **Every GPT-Live 1.1 voice has a preview.** Settings › Voice now plays a short sample for all 14 new voices (Rivage, Hanul, Rio, Miko, Neer, Rindo, Haetsal, Leher, Tinta, Aube, Sonna, Himari, Bubbie, Viola), each saying the same line as the others, with a one-line note on how it sounds. The list runs from the deepest voice to the highest so you can browse by ear. *(Mac app)*

## Mac 0.3.6 and Plugin 0.3.14 — Honest follow-ups, approvals by voice, one task per project

- **You can see whether a task actually read what you added.** When you add something to a running task, its card shows it as *queued* until the task really reads it, then *read by the task*. If the task finishes or is stopped first, the card says so, and the voice tells you and offers to start it as new work instead of letting it drop. *(Plugin and Mac app)*
- **Answer an approval out loud.** When a task stops for your OK, the voice asks once what it wants to do. Say "approve" (or "approved") or "no"; "for the rest of this task" works when Hermes allows that. It only says it's approved after Hermes confirms it. Speakeasy never approves anything permanently, by voice or from the card. With two approvals waiting, a plain "yeah" gets asked which one. The Approve and Deny buttons still work. *(Plugin)*
- **Two tasks don't edit the same project at once.** If you ask for a change to a repo or project that a running task is already changing, the new task waits and the voice tells you which task is in the way. You can let it wait, stop the other one, or start it anyway. A waiting task shows *Waiting* on the board with what it's waiting for, and starts by itself when that task ends. Questions and lookups never wait. *(Plugin and Mac app)*
- **Tapping an answer on a question card sticks.** Picking an option (like "Do it all") marks the question answered right away, and a slow reply from the task no longer puts the question back up. *(Plugin and Mac app)*

## Mac 0.3.5 and Plugin 0.3.13 — Look at this

- **Let Hermes see your screen when you want it to.** A new eye next to the mic turns screen sharing on for the call; press it again, press ⌃⌥S, or say "stop looking at my screen" to turn it off. It's off at the start of every call. While it's on, each new request takes a picture of the window you're working in along with your words, so "what's this error?" or "reply to this email" just works.
- **Drop or paste pictures and files onto the panel.** A screenshot, a photo, a PDF or a log goes with your next request (up to 3 at a time), whether or not screen sharing is on. They wait as small thumbnails you can remove.
- **Ask to look while it's off and it waits for you.** Say "look at my screen" with sharing off and the eye lights up; turn it on within 30 seconds and your request goes ahead with your screen.
- **Private by design.** Your screen and files go only to your own Hermes, never to the voice model. Speakeasy never captures its own windows, password managers or a password you're typing, and anything you share never goes into a group chat or a channel you didn't name. The first time a request takes your screen without you mentioning it, you'll hear which window went.
- **Needs Screen Recording permission** for the screen part (Settings › General › Screen). macOS asks Speakeasy to relaunch after you allow it, and reminds you about once a month.

## Plugin 0.3.12 — A live status that keeps you company

- **The status line tells you what's actually happening.** While a request runs, the agent now gives you a fresh update at every real step — starting or finishing a piece of work it handed to a helper, a build or big search kicking off and landing, a change of plan, and the moment before it writes the answer — with the specifics (names, numbers, files) of what it's thinking through, researching, and running in the background, not just "working on it." *(Plugin)*
- **No more filler.** It skips an update that would only repeat the last one and never pads with "still working," so each line is new and worth reading. *(Plugin)*

## Mac 0.3.4 and Plugin 0.3.11 — Clear an answer without opening it

- **Every new answer has an ×.** Tap it (or long-press the answer and pick Dismiss) and it leaves New answers without opening; it's still in Done today if you want it later. *(Mac app and iPhone app)*
- **"Lights off" doesn't leave a new answer behind.** A home command that Home Assistant carried out is already counted as read: you heard "done" on the call. Anything else, including a home request Hermes handled itself, still shows up as usual. *(Plugin)*
- **More voices with ChatGPT sign-in.** The voice picker lists the newer ChatGPT voices that work with a ChatGPT sign-in. *(Plugin 0.3.10 and Mac app)*

## Plugin 0.3.9 — One sentence, one task

- **A question no longer starts two tasks.** The voice sometimes passes one sentence along twice, the whole thing and then a scrap of its ending a moment later. The second copy used to start its own task (and its own chat thread). Now a repeat of what you just asked is folded into the first request.
- **Answering a question once means once.** Tapping an answer twice, or tapping it and also saying it, sends it to the task one time.

## Plugin 0.3.8 — Follow-ups to a chat thread stop failing

- **"Turn it down a little" after a request that ran in a chat thread works again.** On a live Hermes, a refused turn ends without saying it failed; the refusal only shows on the run itself, so 0.3.7's fallback never kicked in and the follow-up failed. Speakeasy now checks the run, then says the request in the thread (or runs it with the chat's context) as intended.

## Mac 0.3.3 and Plugin 0.3.7 — Set things aside, links you can click, chats picked up in place

- **"Not needed" on anything waiting for you.** A question, option set or approval you've already handled (or don't want to deal with now) can be set aside with one tap, from the board or the job. It leaves Needs you, shows in Done today as Set aside, and the work waiting on it is told to stop asking. You can also just say "that's already handled" or "never mind that one."
- **Links you can click.** When an answer is about something you'd open (an event page, a livestream, an article, a product, tickets), the agent now includes the real link. Rows in lists and schedules open their page (look for ↗), and links in the report are clickable.
- **Picking up a chat thread from voice works inside the thread.** A voice request for an existing Discord or Telegram thread is now said in that thread, through Hermes' webhook platform, so the agent has the whole conversation and replies there; the call still hears the answer. Without a thread or the webhook platform, it runs on its own with the chat's recent messages, as in 0.3.6.

## Plugin 0.3.6 — Voice requests for an existing chat work again

- **Picking up a chat thread from voice works on current Hermes.** Recent Hermes versions no longer let outside apps add a turn to a Discord or Telegram conversation, so a voice request meant for an existing chat failed in about a second and the voice could only say it didn't go through. Speakeasy now runs that request as its own task, gives it the chat's recent messages so it knows what you were talking about, and still posts the answer into that chat. No Hermes changes are needed.

## Plugin 0.3.5 — Status questions get real answers, and the voice talks like a person

- **"How's that coming along?" gets a real update.** A short status question said right after a request used to get glued onto that request and sent to the task as more work, so the voice just said it had passed it on. Status questions now stand on their own. The routing model also recognizes looser ones ("you seem stuck", "what's it doing?"), and the answer comes from what the task is actually doing right now (reading files, editing code, running commands, what it last wrote), not just "I'm on it."
- **No more "Status unconfirmed" on queued follow-ups.** A follow-up waiting its turn behind work already running in the same conversation now stays marked as queued instead of going stale after 90 seconds.
- **Less script, more person.** The voice's instructions no longer contain sample lines it repeated word for word ("On it", "got it, adding that", "Your draft is on the card…", "It will come back as one answer"). It's told how to sound, the way a sharp friend at the next desk would react to that particular ask, and to never reuse the same wording on a call.

## Plugin 0.3.4 — Acknowledgements that fit the ask

- **No more stock "On it."** When you hand off a task, the voice now acknowledges it in its own words about what you actually asked for, and doesn't repeat the same phrase from one request to the next.

## Mac 0.3.2 — The answer leads, pictures fold away

- **A picture no longer hides the answer.** When a finished answer also came with a screenshot, the app treated it as something waiting for you to look at: the job said "Needs you", the picture filled the view, and the actual answer sat far below. Now it's a new answer: the headline and any scoreboard, numbers or list come first, then the report, and the picture folds into a one-line "1 picture" row you can open.
- **Projected scores.** Scoreboards show a "Projected" tag when the numbers are projections rather than a live or final score.

## Plugin 0.3.3 — A picture of the answer, even when the agent didn't draw one

- **Answers with numbers get a picture.** When a finished answer has data but the agent didn't draw a card (tasks continued in a chat thread never could), Speakeasy now draws one from the answer itself: a matchup or projection as a scoreboard, a few numbers as tiles, things compared side by side, a trend as a chart, or a short list of picks. It picks whatever fits what the answer says, never a format tied to a topic, and uses only names and numbers that are in the answer.

## Plugin 0.3.2 — Quick "on it", and answers you can use at a glance

- **"On it" comes right away again.** After 0.3.1 the voice could take 6–7 seconds to acknowledge a request, and sometimes handed the same request off twice. It's now cued to acknowledge the moment work is handed off, and a repeat of the same words within a few seconds is ignored.
- **Data gets drawn, not screenshotted.** When an answer has data, the agent now draws the part that matters with the app's cards (a matchup with both scores, key numbers, a short list, a chart) instead of a screenshot of the page, which got cut off in the panel. Pictures are for when the look itself matters, they never replace the answer, and a website's home page never counts as one.
- **Recommendations fit you.** For things to do, places or shows, the agent checks what it knows about your taste first, leads with what fits, says why, and drops generic picks. If it doesn't know your taste yet, it asks.

## Mac 0.3.1 and Plugin 0.3.1 — A calmer panel you can scroll, and really no more "mm-hmm"

- **The panel scrolls.** With several cards open you could get stuck in the middle with no way to reach the rest. The board and every job now scroll all the way with the trackpad or mouse wheel, and the panel never grows past your screen. Page Up/Down, Space and Home/End scroll too, and the arrow keys keep the selected job in view.
- **The board stays a board.** Cards no longer pile up underneath it. Each card lives in its job: open "Weekend weather" to see the forecast, or "Weekend events" to see the pictures.
- **One card open at a time.** Inside a job, the most relevant card is open and the rest fold into one-line rows; click a row to open or fold it. Big pictures are capped in size; click to see them full size.
- **An obvious way back.** Every job has "‹ All jobs" at the top left, and Esc still steps back.
- **No more "mm-hmm" (plugin, already live).** Not after handing work off, and not out of nowhere while something runs. With ChatGPT voice, the background notes Speakeasy gives the voice (work started, how a task is going, where it went, what's on your screen) were delivered as things to answer, so the voice replied to each one with "Mm-hmm", even when nobody was talking. Those notes now wait and go along with the next thing the voice actually says.

## Mac 0.3.0 and Plugin 0.3.0 — Your work as jobs, with the facts to decide

- **The orb.** Click the orb (or press Esc) and the panel folds into a small orb; the call keeps going. Its color tells you what's up at a glance: grey when idle, blue while you talk, violet while something runs, a blue dot for a new answer, amber with a count when something needs you, red when something failed. The panel header uses the same orb, and the line next to it says why ("2 need you", "1 new answer").
- **A board of jobs, not a pile of tasks.** Open the orb and your work is grouped by job: what needs you first, then new answers, what's running, and what's done today. Follow-ups to the same conversation stay in one job, even across days. Arrow keys move, Return opens, Esc goes back.
- **Decisions you can actually make.** When Hermes needs a call from you, it now explains what's going on, why it recommends an option, and what each option does: its risk, and whether you can undo it. Simple, low-risk calls get an **Approve** button right on the board; anything that spends money, sends something as you, publishes, or deletes always opens the options first.
- **Pictures, not walls of text.** Results can come with timelines (what ran when, and where it broke), before and after, where something goes, a grid of your devices, check tiles and charts. Long reports show their checks, what shipped, and what's still open as a board. Everything drawn comes from real data.
- **Answers get their own place.** A question with nothing to decide shows up under New answers with a small preview (the temperature, a chart, which option won). Open it to see the visuals first, then the details.
- **Talk to what you're looking at.** With a job open, anything you say goes to that job unless it's clearly about something else. A "Talking to" label shows where your words go.
- **Watch it work.** A running job shows its steps as they happen, with Stop and "Tell it something".
- **A calmer panel on long calls.** Finished tasks take one short line, and older ones fold into "+N earlier".
- **No more "mm-hm" (plugin).** With ChatGPT voice, the voice played a canned "mm-hm" every time it handed work off. That filler is off.
- **Fixes (plugin).** Some long answers were saved twice; they're now kept once. When a task had no spoken summary, the read-out could pick a sentence out of context or repeat one; it now reads the opening and only clear warnings.

## Plugin 0.2.55 — Long answers come sorted

- **Long reports are sorted into parts.** When a task finishes with a long answer, Speakeasy sorts it into what kind of work it was (a build, a setup, research, options, or a plain answer) and pulls out its parts: the checks it ran, what's now done, what's still open, and its main sections. The answer itself arrives exactly as before; the sorting happens right after and never delays it. Apps use it to lay a report out as panels instead of a wall of text.
- **Task plans fit real work.** A task's plan now reads like the work most people hand off ("Find the cause", "Fix and test", "Deploy", "Your review"), not like booking a trip.

## Plugin 0.2.54 — Tasks come with a plan

- **Every task you hand off gets a short plan.** When a task goes to Hermes, it's laid out as three to five steps ("Compare flights", "Draft itinerary", "Your review"). Steps are checked off as Hermes reports progress. Apps that show a task's progress line use it; nothing changes in how tasks run.

## Mac 0.2.18 — Listening mode (beta), and calls that actually hear you

- **Listening mode (beta).** Turn it on in Settings › General › Beta. During a call, tap the ear button with the call's controls (or use the menu bar, or a shortcut you set in Settings › Shortcuts): the call pauses and Speakeasy transcribes the room on your Mac without answering. Nothing leaves the Mac and nothing is billed while it listens. Turn it off (or press Resume) and the call picks up again knowing the last 30 minutes of what was said: ask about it ("what did Sam say the deadline was?"), ask for something ("email Dana what we agreed"), or say nothing and it stays quiet, keeping what it heard until you ask. It stops when the call ends, after 2 hours, or when the Mac sleeps; Discard drops what it heard. Needs macOS 26 and the Hermes plugin 0.2.51. When the room text hits its size limit, the oldest lines go first and the newest stay.
- **iPhone: calls stop coming up deaf.** Talk-while-connecting is off on iPhone: its listener and the call fought over the phone's one mic and could leave the call's audio switched off for later calls. Every call now starts with the phone's call audio reset. A tap tells you when to talk.
- **"Listening" means it's listening.** While connecting, it says Listening only once sound is actually coming from the mic. If none arrives within a second and a half, the call takes the mic itself.
- **A call that doesn't connect says so.** Before, a call that never finished connecting sat on "Listening" forever. Now it retries once after 12 seconds, then says it couldn't connect so you can tap again. Where it got stuck is logged.
- **Old questions stop greeting you.** An unanswered question stays pinned for 20 minutes, then lives only in its task. Closing a card or answering one is remembered across launches. On iPhone, a question's answers can now be tapped.
- **iPhone: an Action Button call never ends up behind the app with no mic.** The call starts only once the app is fully in front (iOS gives the mic only to the app in front). If the mic is lost while the app is in the background, it says so and fixes itself the moment you open the app, instead of reconnecting in a loop.
- **iPhone: no more "Fixing the mic…" loop at the start of a call.** The mic check judged the mic dead 2.5 seconds after the call took it over from the talk-while-connecting listener, before the phone's call audio (and AirPods' call mode) had started, then reconnected twice. A phone now gets 7 seconds for the first sound, the listener fully lets go of the mic, and a repair reconnects without the listener. Repairs log what the check saw.
- No more "didn't get that" while a task is clearly still working. It showed 45 seconds after any follow-up or status question that didn't start a new task.

## Plugin 0.2.53 — Tasks rename themselves as they grow

- **A task's name keeps up with it.** When you add to a task ("now make it a weekend in Porto"), it gets a new name for what it has become. Quick answers get a proper name too, not just your first few words.

## Plugin 0.2.52 — Talk to what you're facing (Vision Pro)

- **The Vision Pro room view can tell Speakeasy which task you're facing.** Say something without naming a task ("make it lighter") and it goes to the task in front of you. "New thing" or "something else" still starts fresh. The voice is told what you're looking at, so "what is this?" gets answered right away.

## Plugin 0.2.51 — Listening mode

- **A call picks up after listening mode knowing what was said in the room.** The room transcript (sent with the resume) reaches the voice as background (never as instructions), and any task from that call gets it too, except in posts to a chat thread. Listening again later in the same call adds to it. Questions about the room are answered by the voice right away; a request that sounds like work still goes to Hermes. If you say nothing, the voice stays quiet and keeps the room as context until you ask it something; it never offers things up on its own.
- **Speakeasy never keeps the room transcript.** It isn't written to the call log, recent conversations or Tune (Hermes keeps what its tasks from that call received, like any task), it's dropped when the call ends (or 15 minutes after it's paused), and passwords, keys, card numbers and the like said aloud are redacted before anything leaves the Hermes machine.

## Plugin 0.2.50

- Mic reports from the app can carry the phone's audio state, so a deaf call says why.

## Plugin 0.2.49 — Talk it through first

- **Ideas get a real reply, not a task.** "What do you think about…", "what about the idea of…", "I could make…", "I was thinking…": the voice answers with its own take. Work starts when you ask it to go do something (look it up, research, build, send, book) or say go ahead.
- **"Just talk to me" mode.** Say you're just ideating, or "don't build anything", and nothing goes off to your agent until you ask for something.
- **Reactions aren't requests.** "What, bro", "hang on", "yeah" no longer start a lookup. A "yeah" to something the voice just offered is a go-ahead, and the offer goes along with it.
- **No more humming into silence.** The voice is told to leave pauses empty: no "hmm", "mm" or "mm-hm".

## Plugin 0.2.48 — Talks like a person, and tells you where things are

- **Less "On it", "Checking", "One sec".** Quick questions get the answer, nothing before it. Bigger asks get a short, natural reaction, or none.
- **"You working on that?" gets a real answer.** Status questions are answered right away from what the task has done so far. Before, they were sometimes sent into the task itself, queued behind the work they asked about, and treated as a new request once it finished.
- **Tasks that continue an earlier chat now report progress.** Their updates ("ratings are in, checking fares next") reach the app and the voice. Before, only a narrow format got through, so a long task in an earlier conversation stayed silent until it finished.

## Mac 0.2.17 — Questions you answer in one tap

- **Question cards.** When a task needs a decision, it shows the question with numbered answers and marks the one it would pick. Tap one, press 1–4, or just say it, and the same task carries on. When the choice is something to look at (layouts, places, products), each answer shows a picture. A question stays on screen until you answer it.
- **Choose where voice work goes** (Settings › Where work goes): everything in one place, your default chat plus threads in channels you approve, or sorted into channels by topic.

## Plugin 0.2.47 — See the options

- **Questions about something visual come with pictures.** When the choice is between layouts, designs, photos, places or products, Hermes attaches a screenshot of each option. The card shows them side by side, numbered, and the voice says "they're on screen". Pictures go through the same safety checks as any other task image.

## Plugin 0.2.46 — Questions you answer in one tap

- **When a task needs a decision, it asks you.** Instead of guessing or burying the question in its answer, Hermes ends with one short question and 2–4 options, marking the one it would pick. The voice reads it out ("Quick question: which layout should I build? Three tiers, one plan, or a comparison table. I'd go with three tiers.").
- **Answer however's easiest:** say it, tap an option on the card, or press its number (Mac app, next release). The answer goes back to the same task, which carries on from there.
- In chat threads, the question appears as a numbered list, so you can reply there too.

## Plugin 0.2.45 — Choose where voice work goes

- **New setting: where voice work goes.** Three choices:
  - **One place:** everything goes to your default chat, and Speakeasy never continues a conversation anywhere else.
  - **Default, plus approved threads** (the new default): new work goes to your default chat. A follow-up to something already running in one of your approved channels continues in that thread.
  - **Sorted by topic:** the old behavior. New work goes to the approved channel whose description fits.
  
  Change it with `hermes voice where single|home|topic`, or in the Mac app's Settings (next Mac release).
- **Speakeasy only continues chats you've approved.** It no longer picks up a thread in a channel you never added.

## Plugin 0.2.44 — You hear the heads-up

- **Problems and heads-ups are always said out loud.** When a task's answer has no line written for speech, the voice used to read only the first two sentences, so "the old script broke, I worked around it" further down was never said. Now it reads the opening line plus anything that broke, failed, or needs you.
- **A mid-task warning is spoken right away** instead of waiting its turn behind a routine "starting now" update.

## Plugin 0.2.43 — No more "which channel?"

- **Speakeasy never stops to ask where something goes.** A place in your request ("in the kitchen") is no longer mistaken for a channel name. A channel you name that isn't set up is ignored, and the task goes where it normally would. Two named channels: the first one wins.
- **"Fix that thing it mentioned" continues the right task.** Routing now sees what each recent task told you, not just what you asked it. Pointing back at something a task said, even one that already finished, continues in that task's thread instead of opening a new one.
- **Fixed: a request sometimes reached Hermes twice over** ("dim the lamps dim the lamps"). Keeping the line open after a pause no longer re-adds the words you'd already said.

## Plugin 0.2.42

- **Plainer wording when an email draft is ready:** the voice tells you the draft is on the card and to tap Send once you've read it.

## Plugin 0.2.41 — Lets you finish your thought

- **"Hey" no longer starts a task.** A greeting on its own gets a hello back, never "On it".
- **A breath mid-thought doesn't send half a request.** After you pause, Speakeasy waits a moment (about a second and a half) before starting work. If you keep talking, what you add joins the same request instead of becoming a second task. Light switches and quick follow-ups to running work stay instant.
- **The voice is told to wait for the actual ask** when you're building up to it over several sentences.

## Plugin 0.2.40

- **Finished quick answers clear properly.** Weather lookups, light switches and other instant answers used to linger in the list after you cleared finished work. Now they go away with everything else, including old ones that were stuck.

## Mac 0.2.16 + Plugin 0.2.39 — Says why a task failed

- **The reason, not just "Work failed".** When Hermes can't do a task because its model provider is out of credits, rejected its key or sign-in, is rate-limiting it, or doesn't have the model it's set to use, the task reads "Work failed · Out of credits" (or the matching reason), its detail says what to do (usually top up, or run `hermes model` on the Hermes machine to switch), and the voice says it too. The chat notice and the Mac notification carry the same sentence.
- **Your provider's own error text stays private.** Speakeasy only recognizes known kinds of failure and describes them in its own words; anything it doesn't recognize points you to Hermes' error log on the Hermes machine instead of guessing.
- **No false "mic isn't working".** The panel only says it's listening once your voice is actually getting through. A pause after you've spoken (AirPods go silent between words) is no longer mistaken for a dead mic, and a mic that really stops is reopened automatically before Speakeasy tells you.
- **Your first sentence isn't cut off at connect.** If you start talking while the call is still connecting, Speakeasy finishes hearing that sentence before the call takes over the mic.


## Mac 0.2.15 + Plugin 0.2.38 — Talk while it connects

- **Speak as soon as you tap.** Before the call has fully connected, Speakeasy is already listening and shows a live transcript. Once it connects, those words become your first request, so "dim the den lights" works with no waiting and no repeating.
- **Stays on your Mac.** Those first words are transcribed on the device by macOS speech recognition; macOS asks for permission once. Mute before the call connects and the words are dropped.

## Plugin 0.2.37

- **"Home" says which channel it is.** In the list of places to post results, the home channel now reads "Discord · Home · Your Server / general" instead of just "Discord · Home", so it's clear your general channel is already there.

## Plugin 0.2.36 — Every token price is live

- **Any token or stock by name.** "how's the Zephyr token today", "where's Aerodrome at", "Snowflake shares?" now read the live price and show a price card, not just the handful of big names. Tokens come from CoinGecko, with Coinbase as a backup when CoinGecko is busy.
- **No more stale prices.** If a price can't be read live, the question goes to a full task that looks it up properly, instead of reading an old number from a web search.
- **Tiny prices read right:** $0.0000044, not $0.00.

## Mac 0.2.14 — Answers you can see

- **Cards in the panel.** Quick answers and tasks now show a card next to the spoken answer: a stock with its day chart, the weather with hourly and 7-day forecasts, a game with both teams' logos, a place with its map, a day's schedule, a flight, a package's tracking, a recipe, a side-by-side comparison, and more (41 kinds). The newest one appears on the call screen while it's being said and stays in the task.
- **Tap to open the real thing.** A place opens in Maps, a link opens in the browser, a stock opens its quote page.

## Plugin 0.2.35 — Cards behind every quick answer

- **Prices answer in under a second, with a chart.** "Where's Apple at", "how's ETH today", "what's the S&P doing" read live stock and crypto prices (no web search) and come with a price card: ticker, today's change and the day's chart.
- **Every quick answer now carries a card** for the apps to draw: the weather with hourly and 7-day forecasts, a game with both teams' logos and the score, a league's slate of games, a clock showing there and here, the math worked out.
- **Full Hermes tasks can show cards too.** When an answer is better seen than heard (a place, a route, a flight, a package, a day's schedule, a comparison, a recipe, a draft), the task adds a card alongside its answer. 41 card types in all. Apps that don't know a card type yet simply skip it.

## Plugin 0.2.34

- **Home city is exact.** Neighborhood names the map service doesn't know (or shares with another town) no longer land somewhere else: `hermes voice fast home "Neighborhood, City (lat, lon)"` pins it.

## Plugin 0.2.33

- **Instant clock, date and math.** "What time is it in Oslo", "what's the date", "what's 18 percent of 240" are answered on your machine with no search: under a tenth of a second, always right.
- **Weather in about three seconds,** from live forecast data rather than a web search: now, today, tomorrow, the week, and the next 24 hours. Set a home city for questions that don't name one: `hermes voice fast home <city>`.
- **"Who's playing Monday night?" and other league questions** ("any hockey games tonight", "who won the baseball games last night") now come from the live scoreboard in about two seconds.
- **Quick answers start looking things up while the request is still being sorted,** saving about half a second each.

## Plugin 0.2.32

- **Quick answers in a couple of seconds.** Simple public questions ("how tall is…", "who owns…", "what time is sunset") are answered from one web search instead of a full Hermes task. Anything the search doesn't clearly answer still goes to Hermes, so it never guesses.
- **Sports scores, instantly.** "Did the Mets win?", "when do the Knicks play next?" come straight from live scoreboard data in about a second and a half: final score, who won, home or away, next game.
- **Optional Jev routing.** Connect TypeSafe's Jev through Venice, OpenRouter or TypeSafe and every request is sorted (home, quick answer, Hermes task) in about half a second, instead of the several seconds the routing model takes. Off by default; `hermes voice fast jev venice` turns it on. Without Jev, plainly worded questions still get the quick lane.
- `hermes voice fast` shows and changes all of this.

## 2026-10-01 — plugin 0.2.31

**What it says is what happens**
- When a task is running in its own chat thread, whatever you say about it on the call now goes into that thread, as if you'd typed it there: answering its question ("the 4:15 works"), adding something, or telling it to hold off. The voice only says it passed something on after it actually got there, and tells you plainly when it couldn't.
- The task card follows a thread task's progress ("found Thursday open, pulling times") instead of switching to "Status unconfirmed" after a minute and a half.
- "Show me the options when you have them" reaches the task as a request for a picture, instead of getting "there's no picture to show".
- A new subject that starts with "and also" becomes its own task instead of being added to whatever you asked just before. A single question, even a long one, stays one task.
- The first-call tour only plays on your very first call. A new Mac, a reinstall or an update no longer replays it.

**More visual**
- When an answer is something to look at or choose from, like open times, options, a place or an order summary, the task sends a screenshot with it and the picture opens in the app during the call.

**Home control**
- Short follow-ups right after a home command ("turn them back on") and short names for devices ("the pendants") stay on the instant path instead of going the slow way through Hermes.
- A request that was heard as two pieces ("Bedroom lamps at forty percent" … "purple") is handled as one sentence.

## 2026-09-30 — Mac app 0.2.13

**Choose where Speakeasy lives**
- A new setup screen lets you keep the Dock icon or hide it and use just the menu bar icon. Switch any time with Settings › General › Show in Dock.

## 2026-09-29 (later) — Mac app 0.2.12, plugin 0.2.26–0.2.30

**Tune your voice from your own calls**
- Settings › Voice brief › Tune from my calls: your Hermes reads the last week of calls and suggests specific edits to what the voice knows about you, each with the moment that prompted it. You tick the ones you want; nothing changes otherwise. Problems a brief can't fix are listed separately.
- Also from the terminal: `hermes voice tune start`.

**Smoother calls**
- Your voice channel now logs every voice task. When the answer lands in a thread, another channel or an older conversation, a one-line "Done: … →" link appears in the voice channel pointing to it. "Still working" and "Stopped" lines use the task's name instead of your exact words.
- The voice no longer reads terminal commands out loud. It tells you there's one to run, and the command shows up in chat as its own message, so a long-press copies just the command.
- Tasks that ran in their own chat thread no longer get stuck on "running" after you hang up or Hermes restarts. The finished answer is picked up from the thread, even days later.
- "What's the status?" is answered right away from what the task has done so far, instead of waiting behind it.
- "No, Hermes does" or "yeah, go" goes to the task you were just talking about, not a brand-new one.
- Half-words and repeats no longer start extra tasks. "Make 'em dimmer" right after a lights command is instant.
- The voice no longer says it can't do something or doesn't have access; it starts the work and lets the result say what happened.
- Settings › About links to this list.

## September 29, 2026

**Talk to your house** — Mac app 0.2.11, plugin 0.2.22–0.2.23
- If your Hermes runs Home Assistant, say "kitchen lights to 30 percent" or "den to 72 and office lights off" and it's done in about a second.
- When it isn't sure which one you mean ("set the thermostat to 68"), it asks once, then remembers your answer for the rest of the call.
- Setup offers it when Home Assistant is found. Settings › Home turns it on or off and picks which devices it can touch. Locks, garage doors and alarms always go through the full assistant.

**Calls feel more natural** — plugin 0.2.24–0.2.26
- "How's it going?" gets an answer right away from what the task is actually doing, instead of waiting behind it.
- Saying "no, that's wrong" or "yeah, do it" continues the thing you're talking about instead of starting something new.
- Half-heard scraps no longer start tasks, and saying the same thing twice doesn't start it twice.
- It no longer announces which thread or channel your work went to.
- Calling back later doesn't open with a list of everything that finished while you were away. Ask and it'll tell you.
- "Make them dimmer" right after a lights command is instant too.

## September 28, 2026

**Updates in place** — Mac app 0.2.10
- Check for Updates now installs the new version for you. You approve each one.

**Pictures and "show me"** — Mac app 0.2.7, plugin 0.2.9
- Pictures a task finds or makes pop up in the call panel. Arrow keys page through them.
- Ask "what does it look like?" and the picture opens on screen.

**Clearer while you wait** — Mac app 0.2.8, plugin 0.2.9–0.2.21
- Progress updates say what the task is doing and found, not "still on it", and they come less often.
- If a request doesn't go through, it says so instead of waiting forever.
- Work picks up in the right earlier conversation by what was said there.
- Choose which of your Hermes models sorts your requests (Settings › Task routing).

## September 27, 2026

**Email drafts and task names** — Mac app 0.2.5–0.2.6, plugin 0.2.5–0.2.8
- Email drafts show as a card. Nothing is sent until you press Send.
- Tasks get real names and a short status while they work.
- Pick your assistant's voice, with samples to listen to.

**Speakeasy 0.2.0**
- Talk to your Hermes agent by voice from your Mac: it hands off work, keeps talking while it runs, and tells you when it's done.
- One-message setup through your own agent, a first-call tour, editable shortcuts, and delivery to your Discord or Telegram channels.
