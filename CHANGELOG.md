# What's new in Speakeasy

Speakeasy has two parts that update separately: the **Mac app** (updates through Check for Updates) and the **Hermes plugin** (updates itself in the background; no restart needed). Each entry says which one it came with.

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

- **Instant clock, date and math.** "What time is it in Tokyo", "what's the date", "what's 18 percent of 240" are answered on your machine with no search: under a tenth of a second, always right.
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
