import { hints, spinner, icon, kb, btnPrimary, btnSecondary, bracket, frames, bounceFrames, fillFrames, shadowText } from '../lib.mjs';

// The LAN scan: last octet ticking through the subnet while the bar fills.
const OCTETS = Array.from({ length: 24 }, (_, i) => String(1 + Math.round(i * 253 / 23)).padEnd(3, ' '));

const top = `
<div style="position: absolute; left: 16px; top: 14px; width: 236px">
<span class="eb" style="display: block; font-size: 5.5px; letter-spacing: .4em; color: var(--tx-3)">#00 · Handheld link</span>
<pre class="ascii" role="img" aria-label="Hermes" style="margin: 9px 0 0; font-size: 6.3px; color: var(--blue)">${shadowText('HERMES')}</pre>
<span class="eb" style="display: block; margin-top: 6px; font-size: 6px; letter-spacing: .62em; color: var(--tx)">Agent<span style="color: var(--tx-3)"> · for 3DS</span></span>
<p style="margin: 10px 0 0; width: 210px; font-size: 8.5px; line-height: 12px; color: var(--tx-2)">The agent on your PC, in your pocket. Chat, approve and talk to Hermes over your home network.</p>
</div>

<div class="mono" style="position: absolute; left: 12px; top: 148px; width: 376px; height: 66px; background: var(--blue); font-size: 7px; color: var(--on)">
<div style="height: 14px; display: flex; align-items: center; gap: 6px; padding: 0 7px; border-bottom: 1px solid var(--hair-on); color: var(--on-3)">
<span style="color: var(--on)">$</span><span style="color: var(--on)">hermes 3ds discover</span><span>--lan</span>
<span style="flex: 1"></span><span>192.168.1.0/24</span>
</div>
<div style="height: 16px; display: flex; align-items: center; gap: 6px; padding: 0 7px">
${spinner('var(--on)', 7)}
<span>probing 192.168.1.${frames(OCTETS, '4.8s', 'width: 3ch')}</span>
<span style="flex: 1"></span>
${frames(fillFrames(14), '4.8s', 'color: var(--on-2)')}
</div>
<div style="height: 17px; margin: 0 3px; display: flex; align-items: center; gap: 6px; padding: 0 4px; background: var(--on); color: var(--blue)">
<span style="font-weight: 700">✓</span>
<span style="width: 62px; font-weight: 700">RANDOM-PC</span>
<span style="width: 104px">192.168.1.42:8642</span>
<span>hermes 0.21.5</span>
<span style="flex: 1"></span>
<span class="eb" style="font-size: 5px; padding: 2px 4px; background: var(--blue); color: var(--on)">Paired</span>
</div>
<div style="height: 15px; display: flex; align-items: center; gap: 6px; padding: 0 7px; color: var(--on-3)">
<span>·</span>
<span style="width: 62px">LIVING-ROOM</span>
<span style="width: 104px">192.168.1.17</span>
<span>no hermes</span>
</div>
</div>

${hints([['A', 'Connect'], ['X', 'Scan code'], ['Y', 'Type address']], 'v0.1 · Old 3DS')}
`;

const bottom = `
<div style="position: absolute; inset: 0; padding: 9px 10px 8px; display: flex; flex-direction: column; gap: 7px">
<div style="display: flex; align-items: center; justify-content: space-between">
<span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--ink-3)">#01 · Connect</span>
<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 7px; color: var(--ink-3)">${spinner('var(--pbrand)', 7)}1 PC found</span>
</div>
<h2 class="disp" style="margin: -1px 0 0; font-size: 25px; color: var(--ink)">Pick your Hermes</h2>

<div style="display: flex; flex-direction: column; background: var(--paper-2); border: 1px solid var(--rule)">
<a href="Chat.dc.html" style="position: relative; height: 34px; display: flex; align-items: center; gap: 8px; padding: 0 8px; background: var(--pbrand-soft); outline: 1.5px solid var(--pbrand); outline-offset: -1.5px; color: var(--ink)">
<span style="color: var(--pbrand); display: flex">${icon('pc', 15)}</span>
<span style="flex: 1; display: flex; flex-direction: column; gap: 3px"><span style="font-size: 10px; font-weight: 700; letter-spacing: .02em">RANDOM-PC</span><span style="font-size: 7px; color: var(--ink-3)">Paired · last used today, 9:12 PM</span></span>
<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 7px; font-weight: 700; color: var(--pbrand)">Connect ${kb('A', 'blue')}</span>
</a>
<div style="height: 24px; display: flex; align-items: center; gap: 8px; padding: 0 8px; border-top: 1px solid var(--rule); font-size: 7.5px; color: var(--ink-3)">
<span class="mono" style="font-size: 7px; color: var(--pbrand)">${frames(bounceFrames(8, 2), '1.4s')}</span>
Still looking on this network
</div>
</div>

<button type="button" style="${btnPrimary(30)}">
${icon('scan', 13)}
<span style="flex: 1; display: flex; justify-content: center">${bracket('Scan pairing code', 8)}</span>
${kb('X')}
</button>

<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px">
<button type="button" style="${btnSecondary(25)}">${icon('keyboard', 12)}<span style="flex: 1; font-size: 8.5px; font-weight: 500">Enter address</span>${kb('Y', 'ink')}</button>
<button type="button" style="${btnSecondary(25)}">${icon('help', 12)}<span style="flex: 1; font-size: 8.5px; font-weight: 500">How pairing works</span></button>
</div>

<div style="margin-top: auto; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: 1px solid var(--ink)">
<div style="padding: 5px 6px 0 0; display: flex; flex-direction: column; gap: 3px"><span class="eb" style="font-size: 5px; color: var(--pbrand)">01 · On your PC</span><span style="font-size: 7px; line-height: 9px; color: var(--ink-2)">Run <span class="mono" style="font-size: 6.5px; padding: 0 2px; background: var(--ink); color: var(--paper)">hermes 3ds pair</span></span></div>
<div style="padding: 5px 6px 0; border-left: 1px solid var(--rule); display: flex; flex-direction: column; gap: 3px"><span class="eb" style="font-size: 5px; color: var(--pbrand)">02 · Here</span><span style="font-size: 7px; line-height: 9px; color: var(--ink-2)">Scan the code it shows</span></div>
<div style="padding: 5px 0 0 6px; border-left: 1px solid var(--rule); display: flex; flex-direction: column; gap: 3px"><span class="eb" style="font-size: 5px; color: var(--pbrand)">03 · Done</span><span style="font-size: 7px; line-height: 9px; color: var(--ink-2)">It remembers this 3DS</span></div>
</div>
</div>
`;

export default {
  file: 'Main.dc.html',
  title: 'Hermes 3DS: Connect and pair',
  board: '1 · Connect & pair',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
};
