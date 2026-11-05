# Changelog

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
