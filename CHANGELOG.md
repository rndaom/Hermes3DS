# Changelog

## 0.3.0 (2026-11-15)

App and plugin (0.3.0): send a file from the SD card, make, change and delete scheduled jobs, turn skills and tools
on and off, rename and delete sessions, and a notice when a newer release is out. An older plugin does none of
these, and the app says so (the update notice just never appears).

- **Update notice:** about once a day the plugin asks GitHub for this project's newest release (one request to a
  public page; it names no user, device or version) and tells the app, which says so once in the chat when it is
  older. `hermes 3ds doctor` says when the plugin is older. It is a notice only: nothing is downloaded or
  installed. `update_check: false` under `platforms.3ds` (or `THREEDS_UPDATE_CHECK=false`) turns it off.

- **Rename and delete sessions** (Sessions tab): A on a session opens its options: resume (or open, for the one
  you are in), rename, delete (asks first). A deleted session and its messages are removed from the computer for
  good. The session you are in can be renamed but not deleted, and one Hermes is working in waits until it is
  done. Only this 3DS's own sessions can be touched. Changed key: A used to resume at once; it is now A, then A.
  With an older plugin A resumes at once as before.
- **Skills and tools on and off** (Skills tab): X turns the selected skill off or on. Y, or the button beside the
  list's title, shows the sets of tools Hermes can use (web, files, terminal, browser and more); A or X turns one
  off or on. Both are for chats from a 3DS only: Telegram, Discord and the rest keep their own choice, and each
  bot has its own. A tool set you turn off is gone from your next message; one you turn on, and any skill change,
  starts with your next new chat. What is off for every platform stays the computer's to change (`hermes skills`,
  `hermes tools`), and the skill Hermes needs for itself stays on. Changed key: Y used to refresh the skills list;
  the list now refreshes when you switch between Skills and Tools.
- **Jobs from the 3DS** (Jobs tab): X makes a job with two questions, what Hermes should do and when ("every day
  at 9am", "every 2h", "in 30m"). Its results arrive in the chat you made it in. A on a job opens its actions:
  run now, pause or resume, rename, change the schedule, change the prompt, delete (asks first). The top screen
  shows the job's prompt. A prompt over 1000 characters can be read but not edited on the 3DS. Changed keys: A
  used to run the job at once and X paused it; both are now in the job's actions. A one-time job that has run shows as finished: it can be run again, renamed
  or deleted, but not paused or given a new time.
- A notice too long for one line is no longer cut off: it wraps, and stays up longer.

- **Send a file** (More → 3DS → Send a file): browse the SD card's folders, pick a file of up to 8 MB, add a note
  or a question, and send. It appears in the chat as a file card with its upload progress. On the computer Hermes
  gets it as a document: it is told where the file is, short text files are put into the turn, and pictures
  (JPEG, PNG) are looked at like camera photos. The picker opens in the folder you used last.
- The plugin's `welcome` says `upload.files`; a `send` may attach files of any type, not only photos.

Plugin.

- `hermes 3ds doctor` checks the computer's side of the setup and says what to fix, with the command for each:
  the platform switch, the listener and discovery (asked on `127.0.0.1`), a different listener on the port, the
  computer's addresses, the firewall command for this system, paired consoles, streaming, ffmpeg, speech-to-text,
  each bot's plugin copy and switch, away mode, a container, `security: plaintext` and a key file others can
  read. It only looks: nothing is changed, no key is made, no other program is run.

## 0.2.1 (2026-11-14)

App and plugin (0.2.1): fixes only. Either end still works with the other's 0.2.0.

Plugin.

- Bots keep their titles, descriptions, models and pictures on a stock Hermes: the roster reads `profile.yaml` and
  `config.yaml` with Hermes's own `hermes_yaml` (Hermes does not ship PyYAML, which it used before), and says so
  in the log when a file will not parse.
- One bot is made at a time; a second request while one is being made is told to try again.
- Bots are found when the gateway's main agent is itself a named profile (`hermes -p work gateway run`).
- On Hermes 0.22, with two approval cards showing, the newer card waits for the older one instead of answering it.
- Markdown files are sent as `text/markdown` on Python 3.11 and 3.12 too.
- A server key that cannot be loaded no longer leaves the port's lock held.
- Idle connections can no longer pile up on the home port (at most 64 wait to say hello; the oldest makes way), and
  a flood of them on the away port can no longer push out a 3DS that has already said hello.

App.

- Save to SD: a file whose name starts with a dot is saved under a cleaned name like every other file (its
  slashes were kept before, so the name could point outside the files folder).
- A text with more tables than the layout can count is laid out as records instead of crashing.
- Closing the app waits for its network worker to finish (a wait that timed out was taken for finished, so the app
  could go on closing while the worker still ran).

## 0.2.0 (2026-11-06)

App and plugin (0.2.0). Both ends need it for bots; either one still works with the other's older version, with
only the main agent.

- Bots: a **Bots** tab lists the Hermes profiles the gateway serves (Hermes Bot Mode's bots; needs
  `gateway.multiplex_profiles: true`). Each has its own chat on the 3DS and its own Hermes session, and the
  Sessions, Skills, Jobs tabs and the model picker follow the one selected. Replies from the others raise a dot on
  the tab and a count on their row; an approval or question from one brings its chat up. The app remembers the bot
  last open.
- Bot pictures: each bot wears its Bot Mode look in the list, the top bar and its replies: the photo it was given,
  or its shape and colour with Bot Mode's eyes (a profile nobody styled gets the shape and colour Bot Mode picks
  from its name). The plugin draws a small PNG per look.
- New bot from the 3DS: a name and an optional description make a profile cloned from the main agent
  (`hermes profile create --clone`), with the 3ds platform turned on in it, served at once. Making one no longer
  holds up the connection, and asking again after a dropped reply returns the same bot.
- Labels follow the agent: the top bar, the reply heads, "… is working", "Write to …", the camera, the Skills and
  Jobs pages.
- A bot's 3DS chat goes through that profile's own 3ds adapter, which attaches to the main listener (one port,
  one pairing). The profile needs the 3ds platform on (`hermes -p <name> config set platforms.3ds.enabled true`);
  one without it is listed as not reachable and stays closed to the 3DS. Hermes keeps a copy of the plugin in each
  profile: update it there too; a copy of another release is refused, whichever starts first.
- Approval cards and questions from a bot that left the gateway close instead of coming back on every reconnect.
- A reply that arrives whole (streaming off, files, cron results, or replayed after the 3DS slept) now raises the
  unread dot and plays the reply sound, as a streamed one always did.
- Protocol: §4.8, an `agent` field on agent requests and events, and `agents.list` (with `ready` and `avatar`) /
  `agents.create`.

## 0.1.3 (2026-11-06)

Plugin only; the app is unchanged apart from its version number.

- Approvals answer their own request. On Hermes 0.23 and newer each approval card on the 3DS carries the id of the
  command it asks about, so tapping a card that is no longer waiting (it timed out, or `/approve` answered it)
  approves nothing, and the card closes as timed out, instead of answering the next command waiting in that
  session. On Hermes 0.22 approvals resolve oldest first, as before.

## 0.1.2 (2026-11-05)

App only; the plugin is unchanged (0.1.0).

- Sound without dumping the DSP firmware: when `sdmc:/3ds/dspfirm.cdc` is missing, the app copies the firmware from
  the console's own HOME Menu the first time it starts sound (about half a second, once) and saves it for next time.
  Dumping it in Rosalina still works and is the fallback.
- README: a QR code that installs the latest `.cia` with FBI (Remote Install, Scan QR Code).
- Plugin README: discloses the development-only `security: plaintext` mode.

## 0.1.1 (2026-11-05)

App only; the plugin is unchanged (0.1.0).

- Hermes boot logo for the `.cia`: the splash the HOME Menu shows while Hermes starts (both screens, the banner's
  wordmark in white and light blue on the Hermes blue) in place of makerom's stock "Homebrew" one. Built by
  `tools/build_logo.py` when the HOME Menu's logo key is present (see its `--find-key`).

## 0.1.0 (2026-11-04)

First public release.

- 3DS app: streaming chat with formatting, colour emoji, tables and code; tool activity; Stop.
- Approvals and clarify questions on the 3DS, with the notification LED.
- Sessions, skills, jobs, model picker and slash-command grid.
- Pictures, document reader (text, Markdown, code, PDF, Office), audio player; Save to SD; Open on PC.
- Voice messages (hold Y), camera photos, spoken replies, UI sounds.
- Pairing by code with fingerprint check; LAN discovery and call-back; away from home through Tailscale Funnel
  (opt-in: `hermes 3ds away on`).
- HOME Menu icon, banner and `.cia` build.
- Spoken replies are synthesized sentence by sentence while the model writes (`piper-stream`, optional), and the 3DS
  starts speaking within a second or two of the first sentence. Spoken replies are heard, not posted as voice
  messages; the 3DS header shows VOICE while Spoken replies is on.
- `3ds` platform plugin for the Hermes gateway: Noise IK listener, media transcoding, `hermes 3ds pair |
  devices | unpair | away`. Uses only public Hermes interfaces; needs Hermes Agent 0.22.5 or newer.
- Reliable across restarts and dropped links: message ids stay unique after a gateway restart, a reconnect replays
  what the 3DS may have missed, unsent messages show NOT SENT, and stale approval or question panels clear.
