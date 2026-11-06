# Changelog

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
