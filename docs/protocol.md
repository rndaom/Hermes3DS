# Hermes 3DS wire protocol, v1

The 3DS app (client) talks to the `3ds` platform plugin inside `hermes gateway` (server).
The plugin does all heavy work (formatting, transcoding, paging); the 3DS only displays and sends input.

Status: version 1, as implemented by the plugin and app in this repository. It may still change before a 1.0 release.

---

## 1. Transport

| Item | Value |
|---|---|
| Stream | TCP, server listens on `0.0.0.0:7333` (configurable) |
| Discovery | UDP, server listens on `0.0.0.0:7334` (configurable) |
| Away | TCP, server listens on `127.0.0.1:7339` (configurable), reached through Tailscale Funnel + TLS (§2.4) |
| Byte order | big-endian everywhere |
| Text | UTF-8 |

### 1.1 Frames

Everything on the TCP stream is a sequence of frames:

```
+----------------+---------------------------+
| len : u16 (BE) | payload : len bytes       |
+----------------+---------------------------+
```

- `len` ≤ 65535. A frame is the unit of encryption, so it never exceeds one Noise message.
- During the handshake (§3) the payload is a raw Noise handshake message.
- After the handshake the payload is `Encrypt(body)` (secure mode) or `body` (plaintext dev mode).

A **body** is a 1-byte kind followed by data:

| kind | name | data |
|---|---|---|
| `0x01` | JSON | one UTF-8 JSON object (§4) |
| `0x02` | BLOB | blob chunk (§5) |

Maximum body: 65535 − 16 (AEAD tag) − 1 (kind) = **65518 bytes of data**. A JSON message that would be
larger must instead be paged or sent as a blob; the server never emits an oversize frame.

### 1.2 Modes

| Mode | When | Handshake |
|---|---|---|
| `secure` (default) | always, LAN and WAN | Noise IK (§3) |
| `plaintext` | dev only; server config `security: plaintext` | none — first frame is `hello` JSON |

A server in `secure` mode never accepts plaintext and vice versa; there is no negotiation (no downgrade).

### 1.3 Keepalive and timeouts

- Either side may send `{"t":"ping","n":<int>}`; the peer answers `{"t":"pong","n":<same>}`.
- Client pings every 15 s when idle. Server drops a connection silent for 45 s.
- On disconnect the client reconnects with backoff 1, 2, 4, 8, 15 s (cap). Session state lives on the
  server, so a reconnect resumes where it left off (§4.3 `hello` → `welcome` carries current state).
- 3DS sleep (lid closed) closes the socket; on wake the client reconnects.

---

## 2. Discovery and pairing

### 2.1 LAN discovery (UDP)

Client broadcasts to `255.255.255.255:7334` (and to the cached server IP directly):

```
"H3DS?" (5 ASCII bytes) | version : u8 (=1)
```

Server replies unicast to the sender with a single JSON datagram (no framing):

```json
{"t":"here","v":1,"name":"RANDOM-PC","port":7333,"key":"<base64url server static pubkey>","sec":"secure"}
```

Client retries 3× at 500 ms. Manual IP entry is the fallback; the last good server is cached on SD
(`sdmc:/3ds/hermes/server.json`).

Discovery answers are **unauthenticated**; the key in them is only a hint. Trust comes from pairing.

### 2.2 Pairing

The server has a long-term X25519 static key pair (`~/.hermes/plugin-data/3ds/server_key`, created on
first run). Each 3DS creates its own static key pair on first run (`sdmc:/3ds/hermes/device_key`).

1. User runs `hermes 3ds pair` (or the plugin's pair action). The server creates a one-time **pairing
   token** (16 random bytes, valid 5 min, single use) and shows a QR code + text fallback containing:

   ```
   hermes3ds:1?h=<ip>[,<ip>...]&p=<port>&k=<base64url server pubkey>&t=<base64url token>&n=<name>
   ```

2. The 3DS scans the QR (camera, quirc). It now knows the server's address and **authentic** static key.

   *Not implemented yet:* the app has no QR scanner, so `hermes 3ds pair` prints only the manual code below.
3. The 3DS connects and runs the Noise IK handshake (§3) with `pair` in the first payload.
4. The server checks the token, stores the device's static public key in its device list
   (`~/.hermes/plugin-data/3ds/devices.json`: key, name, paired-at, last-seen), and burns the token.
5. All later connections use the same handshake without a token; the server accepts only known device keys.

Pairing is accepted only on the LAN listener and only from a private (RFC 1918 / ULA), loopback or link-local
source address. Anywhere else, including every connection on the away listener (§2.4), a `hello` with `pair`
gets `{"t":"error","code":"pair_lan_only","msg":"..."}`. A code that leaks is then useless from the internet.

Manual code (what the app uses today; also the fallback without a camera, e.g. an emulator): the PC prints an
**8-character code** (base32 of a 5-byte token) and its addresses; the 3DS finds the PC by discovery (or the
user types the host) and the user types the code. Weaker than the QR (key comes from discovery, not the QR),
so the 3DS shows the server key fingerprint (first 8 base32 chars of BLAKE2b(key)) for the user to compare
with the PC.

### 2.3 Call-back (PC dials the 3DS)

Many routers put a WPA2-only device like the 3DS on an IoT/guest Wi-Fi that may not open connections to the
main LAN, while the LAN may still reach it (and UDP to the PC often still gets through). So when a direct TCP
connect fails, the client:

1. Listens on TCP `7335` (or the next free of `7336`–`7338`).
2. Sends to the server's discovery port, from any UDP port, up to 4× at 700 ms:

   ```
   "H3DS<" (5 ASCII bytes) | version : u8 (=1) | port : u16 (BE)
   ```

3. The server answers `{"t":"dialing","port":<port>}` and opens TCP to **the datagram's source address**
   at `port`. The client accepts it (waiting ~4 s in all).
4. From here the stream is used exactly like a client-opened one: the client is still the Noise initiator,
   so the server authenticates it as usual. No new trust is involved.

Server limits: only private/link-local source addresses, ports ≥ 1024, one new dial per source address per
second, at most 4 dials in flight. A client that got `dialing` but no connection reports that the network
blocks both directions. A client that needed the call-back tries it first on later reconnects.

### 2.4 Away from home (Tailscale Funnel)

Outside the home network the 3DS reaches the PC through **Tailscale Funnel**, which runs only on the PC:

```
3DS ──TLS 1.2 (SNI = <machine>.<tailnet>.ts.net)──▶ Funnel ingress ──▶ tailscaled on the PC (terminates TLS)
    ──plain TCP──▶ plugin away listener 127.0.0.1:7339 ──▶ Noise IK (§3) and frames (§1.1), unchanged
```

- Away access is opt-in (`hermes 3ds away on`, `away: on|auto` in the config, or `away_address`); until then the
  plugin binds no away listener, never runs `tailscale`, and tells paired 3DSes to forget their away address.
- The plugin turns the funnel on with `tailscale funnel --bg --yes --tls-terminated-tcp=<443|8443|10000>
  tcp://127.0.0.1:7339` (the first of those ports not used by another serve/funnel handler), reads it back from
  `tailscale funnel status --json`, and takes the public name from `tailscale status --json` (`Self.DNSName`).
  It rechecks every 10 minutes (every minute while Tailscale isn't running or signed in).
- The away listener binds loopback only, so every connection on it came through the funnel (tailscaled dials
  `127.0.0.1`). No PROXY protocol is needed to tell funnel traffic apart. It never accepts pairing (§2.2) or
  plaintext mode, and it caps unauthenticated handshakes in flight at 16 (a new one drops the oldest).
- TLS only gets the stream through the funnel. The client does not verify the certificate: Noise IK
  authenticates the PC by the key pinned at pairing, end to end, so a TLS man in the middle sees only Noise
  ciphertext. The 3DS (mbedTLS 2.28) offers ECDHE with X25519/P-256 and ChaCha20-Poly1305 first, then AES-GCM,
  and resumes with a session ticket on reconnects.

**The away address** reaches the 3DS only over an authenticated session, as
`"away":{"host":"<machine>.<tailnet>.ts.net","port":443}`:

- in every `welcome` (§4.3, so a 3DS learns it when it pairs at home);
- in every `status_r` (§4.6);
- as an event `{"t":"away","away":{...}}` when it changes while the 3DS is connected.

`"away":null` means the PC has no away route (Tailscale not installed, or turned off); the 3DS forgets its
stored address. A missing `away` key means "not known yet" (for example Tailscale is still starting); the 3DS
keeps what it has. The 3DS stores it in `sdmc:/3ds/hermes/server.json` as `"away":{"host","port"}`.

**Client connection order.** Without an away address: as before (direct, then call-back). With one:

1. The home address with a 2.5 s connect timeout. The call-back (§2.3) is tried only when the 3DS is on the
   home address's /24, since only there can it work.
2. If that fails while on the home /24, discovery checks whether the PC moved (DHCP), then retries the home address.
3. The away address: TCP connect (8 s), TLS handshake (20 s), then the normal Noise handshake.

After an away connection works, reconnects try the away route first as long as the 3DS's own IP address is
unchanged. The status bar shows `LAN` or `AWAY`.

Unpairing: `hermes 3ds devices` lists devices; `hermes 3ds unpair <name>` removes one. On the 3DS, pairing
with another server (SELECT) replaces the one it knew.

---

## 3. Handshake (secure mode)

Protocol name: **`Noise_IK_25519_ChaChaPoly_BLAKE2b`** (Noise spec rev. 34), prologue `"hermes3ds/1"`.

```
IK:
  <- s
  ...
  -> e, es, s, ss      (msg 1, client → server, payload = hello JSON)
  <- e, ee, se         (msg 2, server → client, payload = welcome JSON or error JSON)
```

- Each handshake message is one frame (§1.1) with the raw Noise message as payload.
- Msg 1 payload is a `hello` body (kind byte + JSON, §4.3); msg 2 payload is a `welcome` or `error` body.
- After msg 2 both sides `Split()` into two CipherStates; frames are then `Encrypt(body)` with the
  64-bit counter nonce. Any decrypt failure closes the connection.
- Unknown device key and no valid pairing token → server replies with msg 2 carrying
  `{"t":"error","code":"not_paired"}` and closes.

Implementations:
- Server: Python `hashlib.blake2b` + `hmac` and `cryptography` (X25519, ChaCha20Poly1305). No new deps.
- 3DS: Monocypher 4 (`crypto_x25519`, `crypto_aead_init_ietf` + `crypto_aead_write/read`, `crypto_blake2b`
  with HMAC built on top).
- Both are tested against the published Noise test vectors for this protocol name.

Rekeying: not needed at our volumes (nonce is 64-bit).

---

## 4. JSON messages

Every JSON body is an object with a type field `t`. Unknown types are ignored (forward compatibility);
unknown fields are ignored.

- **Requests** from the client carry `id` (u32, client-chosen, increasing). The server's direct reply carries
  the same `id` with `t` = request type + `_r` (e.g. `sessions.list` → `sessions.list_r`) or `t:"error"`.
- **Events** are server-pushed and have no `id`.
- Times are Unix seconds (float allowed). Sizes are bytes.

### 4.1 Errors

```json
{"t":"error","id":7,"code":"bad_request","msg":"human-readable text"}
```

Codes: `bad_request`, `not_paired`, `pair_lan_only` (§2.2), `version`, `not_found`, `denied`, `too_large`,
`unavailable` (the PC can't do it right now, e.g. no desktop session or an older Hermes), `internal`.

### 4.2 Text content

Message text from the server is plain UTF-8 that the plugin has already simplified for the 3DS
(see §6). Rich content is delivered as **spans**:

```json
{"text":"Run tests now?","spans":[[0,3,"b"],[4,9,"code"]]}
```

Span = `[start, end, style]` (links: `[start, end, "link", url]`) over **UTF-8 byte offsets**. Styles: `b`, `i`, `code`, `link`, `h1`–`h3`,
`dim`, `add`, `del`, `quote`, `table`, `th`. Plain messages omit `spans`.

Tables: a Markdown (GFM) table arrives as one row per line with cells separated by a tab (`	`), header row
first; `table` spans the whole block (no trailing newline) and `th` the header row. Cells keep their inline
spans. The 3DS lines the columns up when they fit the width, and otherwise shows each row as a record: the
first cell as a bold title, then `Header: value` lines. While a reply streams, an unfinished row (and a header
whose delimiter row hasn't arrived) is held back so raw pipes never show.

### 4.3 Session start

```json
// client → server (handshake msg 1 payload, or first frame in plaintext mode)
{"t":"hello","v":1,"app":"3ds-0.1.0","device":"Random's 3DS","model":"old3ds",
 "screen":{"top":[400,240],"bottom":[320,240]},
 "caps":["emoji:7fa053d0","voiceq","speak"],
 "pair":"<base64url token, only when pairing>"}

// server → client (handshake msg 2 payload)
{"t":"welcome","v":1,"server":"RANDOM-PC","hermes":"<version>",
 "away":{"host":"random-pc.tail1234.ts.net","port":443}, "upload":{"max":8388608},
 "chat":{ ... current chat state, §4.4 ... }}
```

`away` is described in §2.4 (absent = not known yet, `null` = none); `upload` in §5.1.

Version mismatch → `{"t":"error","code":"version","msg":"..."}` and close.

`caps` lists what the app can display or do. `emoji:<set id>` says it has the colour emoji sprite set with that id
(§6); without a matching one the server sends emoji as `:name:` text in every body, the welcome included.
`voiceq` says it plays voice messages (§4.7 `voice`) one after another, so the server may send a spoken reply as
several short ones while Hermes is still writing; without it a spoken reply is one message, sent when it is complete.
`speak` says it plays `speak` events (§4.4): the server then sends a spoken reply's parts as those while Hermes
writes, and the chat gets no voice message for it. It wins over `voiceq`.
The server remembers the caps for as long as that connection lasts, and the last app's `speak` while none is
connected (a reply spoken then is dropped, not kept for later).

### 4.4 Chat

Each paired device is one Hermes chat (`chat_id = "dev:<fingerprint>"`, DM). Hermes owns the session;
`/new`, `/resume`, `/model` etc. work exactly as on any other platform.

**Client → server**

| t | fields | notes |
|---|---|---|
| `send` | `id, text` | A chat message. Text starting with `/` is a slash command. Reply `send_r {id, mid}`. |
| `send` | `id, voice, text?` | A voice message (§4.4.1). |
| `send` | `id, attach, text?` | Photos with an optional caption (§4.4.1). |
| `stop` | `id` | Interrupt the running turn (same as sending `/stop`). |

**Server → client events**

| t | fields | notes |
|---|---|---|
| `msg` | `mid, role, text, spans?, final, kind?, ts` | New message. `role`: `assistant`, `system`. `kind`: `reply` (default), `notice`, `cron`, `command` (reply to a slash command). `final:false` = still streaming. |
| `append` | `mid, text, spans?, final` | Streaming: add `text` to the end of message `mid`. `spans`, when present, replaces the message's whole span list (`[]` clears it); sent only when it changed. |
| `edit` | `mid, text, spans?, final` | Replace the whole text of `mid` (used when the prefix changed, and on finalize to deliver spans). |
| `delete` | `mid` | Remove message `mid` (a streamed preview Hermes replaced, or one that held only a `MEDIA:` tag). Sent on reconnect if it happened while offline. |
| `typing` | `on` | Agent turn running (`true`) / idle (`false`). The 3DS shows the braille spinner + thinking verb. |
| `heard` | `req, text` | What Hermes' speech-to-text made of the voice message sent with request `req` (§4.4.1). |
| `speak` | `speech, attach` | A part of a spoken reply, a few seconds of speech, sent while Hermes is still writing to an app with the `speak` cap (§4.3). `attach` is one voice attachment (§4.7); the app plays the parts in arrival order, each when the one before has ended, without showing them. Not kept for an offline device. The chat gets no voice message for a reply spoken this way. A reply Hermes speaks in one piece (no streaming) comes as a single `speak`. |
| `tool` | `state, text?` | Tool activity from Hermes' live status: `state:"start"` with a phrase (e.g. `Running echo hi`) when a tool starts, `state:"done"` when it completes. The 3DS shows it as a terminal line with the braille spinner while running. |

Streaming detail: Hermes streams by editing one message (~0.8 s cadence). The plugin strips Hermes'
text cursor (` ▉`) and sends `append` when the new text extends the old one, else `edit`. The 3DS draws
its own caret while `final` is `false`.

Formatting streams with the text: every streaming update carries spans. The line still being written is
closed for display first (`Run **all te` shows `all te` bold before the closing `**` arrives), and markers
with nothing after them yet (`**`, `` ` ``, `[`, a bare `#` or `-` line start) are held back so raw markup
never flashes. The final `edit` carries the spans of the finished text, which win.

#### 4.4.1 Voice messages and photos (client → server)

Both are uploaded first (§5.1); the `send` that refers to them goes only after `blob.put_done`.

```json
{"t":"send","id":21,"text":"","voice":{"blob":"u5k2…","rate":16360,"block":1024,"ms":7310}}
{"t":"send","id":22,"text":"What plant is this?","attach":[{"blob":"u9fq…"}]}
```

- `voice`: the recording as **IMA-ADPCM, mono, `rate` Hz** (the 3DS microphone runs at 16360), in the same
  headerless block layout as §4.7 `audio` (`block` bytes per block, 1024); `ms` is its length. The PC decodes it
  to a 16-bit PCM WAV and hands it to Hermes as a voice message, so Hermes' speech-to-text transcribes it. When
  Hermes reports the transcript (its "echo transcripts" setting, on by default) the PC sends
  `{"t":"heard","req":21,"text":"turn on the lights"}` instead of a chat bubble; the 3DS shows the words under
  its voice bubble. No `heard` means speech-to-text is off or failed (Hermes then tells the model a voice
  message could not be transcribed).
- `attach`: 1 to 4 uploads of `image/jpeg` or `image/png`, handed to Hermes as photos (vision). `text` is the
  caption and may be empty.
- An upload that isn't complete on the PC gets `error` `not_found`; a recording that doesn't decode gets
  `bad_request`.

Voice replies come back as audio attachments (§4.7) marked `"voice":true`, which the 3DS may play as they
arrive. Hermes speaks replies when its voice mode asks for it (`/voice on`: reply to voice with voice). The
`welcome`'s `chat.voice` (boolean, optional) says whether Hermes speaks in this chat now, so the 3DS can send
`/voice on` or `/voice off` to match its own setting.

Offline delivery: messages produced while no device is connected (cron results, a reply that finishes
after the lid closed) go to a per-device outbox (last 50) and are delivered right after `welcome`.

### 4.5 Approvals and clarify

```json
// server → client: Hermes wants to run a dangerous command
{"t":"approval","req":"a7","text":"...","command":"rm -rf build/","description":"delete build dir",
 "actions":[["Allow once","once","primary"],["This session","session",""],["Always","always",""],["Deny","deny","danger"]]}
// client → server
{"t":"approve","id":12,"req":"a7","choice":"once"}        → approve_r {id, resolved:n}
// server → client when resolved (by this device, timeout, or another path)
{"t":"approval_done","req":"a7","choice":"once"}
```

```json
// server → client: the agent asks a question
{"t":"clarify","cid":"c3","question":"Which branch?","choices":["main","dev"],"multi":false}
// client → server: pick a choice (0-based) or answer in free text ("Other")
{"t":"answer","id":13,"cid":"c3","choice":1}
{"t":"answer","id":14,"cid":"c3","text":"feature/x"}
// multi:true questions take several picks at once
{"t":"answer","id":15,"cid":"c4","choices":[0,2]}
{"t":"clarify_done","cid":"c3"}
```

Open-ended clarify has `choices: []`; the client answers with `text`. On a question with choices, typed
text that names them (`2`, `1,3`, or a choice's label) counts as picking them, as on Hermes's other platforms. Pending approvals/clarifies are
re-sent after `welcome` on reconnect. While one is pending the 3DS lights the yellow LED (see design notes).

### 4.6 Structured lists and status

Request/response pairs (`id` echoed). Lists take optional `offset`, `limit` (default 20, max 50).

| request | fields | response data |
|---|---|---|
| `status` | — | `{now, session:{id,title,model,started,msgs,tokens_in,tokens_out,cost}, busy, queued, away?}`. `away` as in §2.4. `model` is what the next turn uses (a `/model` override, else the configured default). `now` is the PC's Unix time; the 3DS clock holds local time, so the app derives its UTC offset from it. |
| `sessions.list` | `offset?, limit?, q?` | `items:[{id,title,preview,last_active,msgs,model,current}]` |
| `sessions.messages` | `sid, limit?, before?` | `items:[{mid,role,text,spans?,tool?,ts}]`, oldest first; `before` = message id for paging back |
| `sessions.resume` | `sid` | ack; the switch result arrives as a `msg` (kind `command`) |
| `sessions.new` | `title?` | same |
| `commands.list` | — | `items:[{name,desc,args,aliases}]`: Hermes' gateway slash commands, from a snapshot shipped with the plugin (`plugin/h3proto/hermes_commands.json`, made by `tools/build_commands.py`) |
| `models.list` | — | `{items:[{provider,name,current,models:[{id,current}]}], model, provider}`: the models `config.yaml` names (default, `providers:`, `custom_providers:`, fallbacks); `current` follows a `/model` override |
| `skills.list` | — | `items:[{name,desc,category,enabled}]` |
| `tools.list` | — | `items:[{toolset,label,desc,enabled,tools:[...]}]` |
| `jobs.list` | — | `items:[{id,name,schedule,state,next_run,last_run,last_status}]` |
| `jobs.action` | `job, op` (`pause`/`resume`/`run`) | `{job:{...}}` |

Responses are `{"t":"<request>_r","id":n, ...data}`. Actions that change the session (`resume`, `new`,
model switch) are performed by sending the slash command through Hermes, so they behave exactly like typing it.

### 4.7 Media

Server → client: a `msg` may carry `attach`, a list of attachments. Its `text` is the caption (may be empty).

```json
{"t":"msg","mid":"m40","role":"assistant","kind":"reply","text":"Here's the plot.","final":true,"ts":1760000000,
 "attach":[{"kind":"image","name":"plot.png","size":183020,
            "view":{"id":"b1","size":48213,"mime":"image/jpeg"},"w":800,"h":600,
            "thumb":{"id":"b2","size":2911,"mime":"image/jpeg"},"tw":120,"th":90,
            "file":{"id":"b3","size":183020,"mime":"image/png"}},
           {"kind":"file","name":"report.pdf","size":1048576,"mime":"application/pdf",
            "file":{"id":"b4","size":1048576,"mime":"application/pdf"},
            "text":{"id":"b5","size":20480,"mime":"application/json"}},
           {"kind":"audio","name":"reply.ogg","size":31877,"mime":"audio/ogg",
            "audio":{"id":"b6","size":66560,"mime":"audio/x-ima-adpcm"},"rate":22050,"block":1024,"ms":6012,
            "file":{"id":"b7","size":31877,"mime":"audio/ogg"}}]}
```

- `kind` is `image`, `audio` or `file` (later: `table`, `code`, `pages`). `name`/`size` describe the
  original. A client should show an unknown kind as `file`.
- `view` (images): the picture for the viewer, transcoded on the PC to fit 800×800 (never upscaled):
  baseline JPEG (quality 85), or PNG when the source has transparency or PNG is smaller (flat graphics);
  `w`/`h` are its pixel size.
  Animated GIFs send their first frame.
- `thumb` (images): the same picture fitting 120×90, always JPEG, for the chat card; `tw`/`th` its size.
- `file`: the original bytes, for Save to SD. Omitted when the original is over 16 MiB.
- `text` (files): a reader preview, when the PC can get text out of the file: text, Markdown and code files
  are read directly; PDF, Word, PowerPoint, Excel, OpenDocument, RTF and EPUB documents are converted to
  Markdown on the PC (anydoc, which Hermes bundles). The blob is JSON
  `{"text":"...","spans":[...],"more":true}`: `text`/`spans` as in §4.2 (Markdown already turned into
  spans; plain text and code files have none), from at most the first 64 KiB of source
  text, cut at a line break; `more` (omitted when false) says the file goes on past that. Scanned PDFs
  (no text layer), encrypted documents and binary files get no `text`.
- `audio` (audio): the sound for the 3DS to play, transcoded on the PC by ffmpeg (any format it reads; files
  sent as voice are tried whatever their name) to **IMA-ADPCM, mono, `rate` Hz (22050)**: Microsoft WAV
  IMA-ADPCM blocks of `block` bytes (1024), concatenated with no WAV header. Each block decodes on its own:
  int16 LE first sample, u8 step index (0..88), u8 reserved, then 4-bit codes, low nibble first, so a block
  is `(block - 4) * 2 + 1` samples (2041) and the last block may be shorter. ~11 KB per second of sound, so
  the client can play it while it downloads and seek by block. `ms` is the length in milliseconds (the last
  block's padding is past it). Audio longer than 15 minutes is cut there and gets `"cut":true`. Without
  ffmpeg on the PC, or when it finds no audio stream, the attachment is a plain `file`.
- `{"t":"blob.open","id":n,"blob":"b3"}` → `blob.open_r`: the PC opens that original with its default app
  ("Open on PC"). Only `file` blobs can be opened.

- `voice` (audio): `true` when Hermes sent the sound as a spoken reply (text-to-speech), not as a file. The 3DS
  may play it as it arrives (the "Spoken replies" setting). With `voiceq` (§4.3) a reply can arrive as several
  `voice` messages, a few seconds of speech each, the first as soon as Hermes has its first sentence; the app
  plays them in arrival order, each when the one before has ended, and may start downloading the ones waiting.
  A message id the app has already spoken (a replay after a reconnect) is not spoken again.
- `speech` (audio, with `voice`): no longer sent; servers before 0.1.0 posted the whole of a reply spoken through
  `speak` events (§4.4) as this message, with the parts' id here. An app that got parts with that id (played or
  not) doesn't speak it again.

Client → server: voice messages and photos, §4.4.1.

---

### 4.8 Bots (several agents on one connection)

A Hermes gateway with `gateway.multiplex_profiles: true` serves its main profile plus every named profile under
`~/.hermes/profiles/` (the "bots" of Hermes Bot Mode, which is a desktop view over the same profiles). Each is an
agent with its own home: config and model, `.env`, SOUL, memory, skills, sessions and cron jobs. The 3DS has one
chat with each.

**The `agent` field.** Requests about one agent's chat carry an optional `agent`, the profile name: `send`, `stop`,
`status`, `approve`, `answer`, `sessions.*`, `commands.list`, `models.list`, `skills.list`, `tools.list`, `jobs.*`.
No `agent` (or `""`, or the main profile's own name) means the main agent, which is what an app from before bots
sends, so it keeps working unchanged. The server runs the request in that profile (its session store, its skills,
its jobs) and echoes the request's `agent` into the reply. Server events for a bot's chat (`msg`, `append`,
`edit`, `delete`, `typing`, `tool`, `speak`, `heard`, `approval`, `approval_done`, `clarify`, `clarify_done`) carry
its `agent`; the main
agent's carry none. A name the gateway doesn't serve is `not_found`; a malformed one `bad_request`; a bot whose
profile doesn't run the 3ds platform (`ready: false` below) is `unavailable` for every request: a profile is open to
the 3DS only once it turns 3ds on. `approve` and `answer` are the exception: they name their prompt (`req`, `cid`)
and are answered even for a bot that has left the gateway, so its cards can be cleared; when a bot leaves, the
server settles its pending prompts itself (`approval_done` with `choice: "timeout"`, `clarify_done`).
Uploads, blobs, `blob.open` and the roster requests below are the device's, not an agent's, and take no `agent`.

On the PC each bot's chat is its own Hermes chat: chat id `dev:<fingerprint>/<profile>` (the main agent's stays
`dev:<fingerprint>`), source `profile` set to the bot, session key `agent:<profile>:3ds:dm:dev:<fingerprint>/<profile>`.
A multiplexed gateway builds one adapter per served profile and admits a profile's turns only through that
profile's own adapter, so a bot's 3ds adapter binds no port: it attaches to the main profile's listener (one port,
one pairing) and shares its state, and the listener hands the bot's messages to it; replies go out through it.
Hermes loads each profile's own copy of the plugin (a cloned profile gets a copy), so the copies meet in a
process-wide registry, and a copy of another plugin release is refused (`plugin_mismatch`, checked whichever of
the two comes up first): update the plugin in every profile. A bot's adapter with no listener to attach to (the
main profile doesn't run 3ds) fails with `no_listener`. Offline replay (§4.4) keeps each event's tag.

| request | fields | response data |
|---|---|---|
| `agents.list` | — | `{items:[{id,title,desc,model,provider,primary,bot,busy,ready,voice?,avatar?}], multiplex}`: the main profile first (`primary`), then the bots. `title` is Bot Mode's name for it (`ui_meta.hermes-bots.title`), else `display_name` from `profile.yaml`, else the main agent's "Hermes" or the name made readable; `bot` says Bot Mode lists it. `busy`: a turn is running in this device's chat with it; `voice`: Hermes speaks its replies there (`/voice`); `ready`: the gateway has a 3DS adapter in that profile, so its turns have a way in (false: requests for it get `unavailable`). `avatar`: a blob ref (§5) to its picture, a 36×36 PNG with a see-through background: the photo Bot Mode set (`<profile>/assets/avatar.png`, `.jpg` or `.webp`, cover-cropped), else Bot Mode's `ui_meta.hermes-bots` `shape` (circle, squircle, pill, triangle, hexagon, cloud, drop) in its `color`, with two eyes; a profile nobody styled, or a generated look (blobatar, sigil, solid) this side can't draw, gets the shape and colour Bot Mode picks from its name. None for the main agent, or without Pillow on the PC. A picture is drawn once per look and its blob reused for a few days; a new look gets a new blob id. Without `multiplex` the list holds only the main agent. |
| `agents.create` | `name, desc?` | `{item:{...as in agents.list}}`. Makes a profile with `hermes profile create <name> --clone-from <main> --no-alias [--description <desc>]` (what Bot Mode's New Agent does with "Copy API keys": config, `.env`, SOUL, skills and memory notes are copied; chats, sessions, cron jobs and messaging channels are not) then turns the 3ds platform on in it (`hermes -p <name> config set platforms.3ds.enabled true`: a clone has no `platforms:` section, and without it the bot couldn't be reached) and has the gateway serve it at once. `name` is turned into a profile id (`Research Buddy` → `research-buddy`, `[a-z0-9][a-z0-9_-]{0,63}`, not `default`, `main` or a name Hermes reserves). Answered beside the device's other requests (it can take a minute), so pings, sends and approvals keep flowing; the same request again within 10 minutes of a success (the reply was lost to a dropped connection) returns the same bot. `unavailable` when the gateway doesn't multiplex profiles or the `hermes` command fails (its last error line is the `msg`); `bad_request` for a bad, taken or reserved name, or one already being made. |

An older plugin answers `agents.list` with `bad_request` ("unknown request"); the app then shows only the main
agent and says the plugin needs an update.

## 5. Blobs

Binary content (images, files, text previews, audio) moves as blobs, referenced from JSON as
`{"id":"b12","size":48213,"mime":"image/jpeg"}`.

1. The client asks: `{"t":"blob.get","id":9,"blob":"b12","offset":0}` (`offset` resumes a transfer).
2. The server replies `{"t":"blob.get_r","id":9,"blob":"b12","size":48213}`, then sends the bytes from `offset`
   in BLOB frames, in order, each carrying at most 32768 bytes:

```
kind 0x02 data:
+------------------+----------------+-----------------+-------------+
| id_len : u8      | blob id (ASCII)| offset : u32 BE | bytes ...   |
+------------------+----------------+-----------------+-------------+
```

3. The transfer is done when `offset + len(bytes) == size`. `{"t":"blob.cancel","blob":"b12"}` stops it
   (no reply). Several transfers may run at once; their frames interleave.
4. An unknown or expired id gets `error` with code `not_found`.

Blob ids are opaque ASCII (at most 32 characters). The PC keeps blobs on disk for 7 days, so an attachment
replayed after a reconnect or gateway restart still loads within that time.

### 5.1 Uploads (client → server)

Photos and voice messages go the other way with the same chunk format:

1. The client picks an id (`u` + 1–31 characters `a-z0-9`, random, so it never clashes) and asks:
   `{"t":"blob.put","id":10,"blob":"u5k2…","size":59871,"mime":"audio/x-ima-adpcm","name":"voice"}`.
2. The server answers `{"t":"blob.put_r","id":10,"blob":"u5k2…","offset":0}`: `offset` is how many bytes it
   already has (0 for a new upload; more after a dropped connection, so the client resumes there; `size` when
   it is already complete).
3. The client sends BLOB frames (§5 layout, at most 32768 bytes each) from `offset`, in order.
4. When the last byte is in, the server sends `{"t":"blob.put_done","blob":"u5k2…","size":59871}` (also right
   after `blob.put_r` for an upload that was already complete).

- A frame at the wrong offset, or for an upload that isn't open, gets
  `{"t":"error","code":"bad_request"|"not_found","blob":"u5k2…","msg":"..."}`; the connection stays up, and the
  client sends `blob.put` again to learn where to continue.
- Limits: `size` at most 8 MiB (else `too_large`). An unfinished upload is kept for 1 hour, a finished one like
  any blob (7 days). Ids belong to the device that made them (another device's id: `denied`).
- A finished upload is an ordinary blob: `blob.get` fetches it back.
- The `welcome` of a server that takes uploads carries `"upload":{"max":8388608}`. Without it (an older PC
  plugin) the client doesn't offer voice messages or photos.

---

## 6. What the plugin pre-processes for the 3DS

- Markdown → plain text + spans (§4.2); tables become tab-separated rows (`table`/`th` spans).
- Code blocks → text + Pygments color spans; diffs → `add`/`del` spans.
- Emoji → colour pictures (`plugin/h3proto/emoji.py`, sprites built by `tools/build_emoji.py` from Twemoji,
  CC-BY 4.0). The plugin does all sequence matching (longest match; ZWJ sequences, skin tones, flags, keycaps,
  subdivision flags; U+FE0F ignored) and sends each emoji in the set as **one code point `U+F0000 + index`**
  (Supplementary Private Use Area-A, 4 UTF-8 bytes; above the font's 16-bit character map, so no clash with
  its U+E000… button glyphs). The app draws that code point as a square sprite a little under the line height,
  vertically centred in the line, advancing its size + 2 px; it measures and wraps it like any character.
  The index order is the sorted Twemoji file list in `plugin/h3proto/emoji_set.json`; its hash is the set id
  (`emoji:<id>` cap, §4.3), and the app reads the same id from `romfs:/emoji.bin`.
  A single code point the font can draw (☆★○●♪←→ ™ © …) stays text unless U+FE0F follows it; U+FE0E keeps text.
- Emoji the set lacks, and every emoji for an app without the matching cap → `:name:` text tokens: a curated
  table for common ones (📌 `:pin:`, ✅ `:check:`, ❌ `:x:`, 🚀 `:rocket:`, 👍 `:+1:`), the lowercased Unicode name
  for the rest (🦩 `:flamingo:`), flags as `:flag_us:`, keycaps as `[1]`; ➡⬅⬆⬇ become the font's arrows.
  Variation selectors, skin tones, ZWJ and tag characters are dropped. For an app without the cap the server
  rewrites sprites to this text when sending (`downgrade_message`, span offsets adjusted).
- Both are applied to every outgoing string (message text before spans are measured, so span offsets are over
  the final text; tool status, titles, previews).
- Images → 3DS-size JPEG/PNG (GIF: first frame); text, code, PDF and Office documents → reader text (§4.7
  `text`; its emoji are always sprites); audio → mono IMA-ADPCM (§4.7 `audio`); later: PDF page images.
- Long text is paged so no JSON body exceeds the frame limit.
