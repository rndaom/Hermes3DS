# Using your 3DS away from home

At home, the 3DS talks to Hermes on your PC over your Wi-Fi. To use it anywhere else (a friend's Wi-Fi, a
café, your phone's hotspot), install **Tailscale** on the PC and turn away mode on. It is off until you do,
because it gives your PC a public address (see *Good to know*). Nothing needs to be installed on the 3DS.

## Set it up (once, about 5 minutes)

1. On the PC, download Tailscale from <https://tailscale.com/download> and install it.
2. Open Tailscale and **sign in**. A free personal account is fine (you can use Google, Microsoft, GitHub
   or Apple to sign in).
3. Turn away mode on: `hermes 3ds away on`. It sets up the Tailscale Funnel and prints the address.
4. Restart Hermes: `hermes gateway restart`.
5. Check it worked: `hermes 3ds away`. You should see:

   ```
   Away from home: ready at your-pc.tailXXXX.ts.net:443 (the 3DS learns it automatically).
   ```

6. Turn on the 3DS **at home** and open Hermes once. It picks up the away address by itself.

That's it. When the 3DS can't find the PC on the Wi-Fi, it connects over the internet instead. The top
right corner of the 3DS screen says **LAN** at home and **AWAY** when it is going over the internet.

## If `hermes 3ds away` shows a problem

| It says | What to do |
|---|---|
| "off. To use the 3DS over the internet…" | Away mode isn't on yet: `hermes 3ds away on` (step 3). |
| "turned on; restart the gateway" | `hermes gateway restart` (step 4). |
| "Tailscale is not installed" | Install Tailscale (step 1), then restart Hermes. |
| "installed but not signed in" or "turned off" | Open Tailscale on the PC and sign in or connect. Hermes retries every minute. |
| "Funnel is not turned on for your Tailscale account yet. Open this link…" | Open the link on the PC, click to allow it, then restart Hermes. |
| "Funnel is not allowed in your tailnet policy" | Your Tailscale account (or whoever runs it) has to allow Funnel: in the admin console, Access controls, give this PC the `funnel` attribute. |
| "HTTPS Certificates are off" | Admin console, DNS page: turn on HTTPS Certificates, then restart Hermes. |
| "Tailscale refused the change (access denied)" | Linux: make your user the Tailscale operator once, as root: `tailscale set --operator=$USER`. Hermes retries every minute. |
| "too old for Funnel TCP forwarding" | Update Tailscale. |
| "waiting for approval in the Tailscale admin console" | Approve this PC in the admin console (Machines). |
| "no name for this PC yet (MagicDNS off?)" | Admin console, DNS page: turn on MagicDNS. |
| Ports 443, 8443 and 10000 are all used | Funnel only offers those three ports and other services on this PC took them. Free one, or pick it with `away_public_port`. |

If `hermes 3ds away` says ready but the 3DS still can't connect away from home: open Hermes on the 3DS once
**at home** after setting up Tailscale (that is when it learns the address; the More tab then shows "away ready"),
and remember the 3DS can't use networks with a sign-in page. The 3DS log (`sdmc:/3ds/hermes/log.txt`) has a `TLS to ...` or `away route failed` line with the
reason. A brand-new Funnel can take a few minutes before the internet reaches it.

## Good to know

- **Pairing only works at home.** A new 3DS must be paired on the same Wi-Fi as the PC. Over the internet,
  the PC only lets in a 3DS it already knows.
- **It stays private.** Tailscale Funnel gives your PC a public address, but everything the 3DS sends is
  encrypted end to end with the key it learned when you paired. Someone who finds the address can't get in
  without a paired 3DS.
- **The PC must be on**, with Hermes and Tailscale running.
- **Wi-Fi on the go:** the 3DS needs plain WPA2 Wi-Fi and can't do sign-in pages (hotel, airport). A phone
  hotspot works well: set it to 2.4 GHz and WPA2.
- **Turn it off:** `hermes 3ds away off` removes the Funnel (and `hermes 3ds away on` brings it back).
- **Settings** (in Hermes' `config.yaml`, under `platforms: 3ds:`): `away: on` turns it on without the
  command; `away: off` keeps it off and the plugin never touches Tailscale;
  `away_port` (default 7339) is the local port Tailscale forwards to; `away_address: host:port` uses a
  TLS tunnel you run yourself instead of Tailscale.
