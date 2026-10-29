import { header, hints, spinner, icon, kb, wing, tabs, frames, fillFrames } from '../lib.mjs';

const badge = (src, solid = false) => `<span class="mono" style="font-size: 5.5px; font-weight: 700; letter-spacing: .06em; padding: 1.5px 3px; ${solid ? 'background: var(--blue); color: var(--on)' : 'border: 1px solid var(--line-b); color: var(--blue)'}">${src}</span>`;

const top = `
${header({ title: 'Sessions · 3 of 41', link: 'LAN' })}

<div style="position: absolute; left: 12px; top: 24px; width: 376px; display: flex; flex-direction: column; gap: 5px">
<div style="display: flex; align-items: center; gap: 6px">${badge('DISCORD', true)}<span class="mono" style="font-size: 6.5px; color: var(--tx-3)">Yesterday, 11:02 PM · 48 messages · qwen3.8-27b</span></div>
<h2 class="disp" style="margin: 0; font-size: 24px; color: var(--tx)">TF2 frag video: render queue</h2>
</div>

<div style="position: absolute; left: 12px; top: 70px; width: 376px; display: flex; flex-direction: column; gap: 6px">
<div style="align-self: flex-end; max-width: 250px; padding: 4px 7px 5px; background: var(--blue); color: var(--on); font-size: 8px; line-height: 11px">Queue the three clips from tonight and render them at 60 fps.</div>
<div style="display: flex; gap: 7px">
<span style="width: 15px; height: 15px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line-b); color: var(--blue)">${wing(9)}</span>
<div style="flex: 1; display: flex; flex-direction: column; gap: 4px">
<div class="mono" style="display: flex; align-items: center; gap: 5px; height: 13px; padding: 0 6px; background: var(--scr-2); font-size: 6.5px; color: var(--tx-2)"><span style="color: var(--tx-3)">┊</span><span style="font-weight: 500; color: var(--blue)">cronjob</span>render-queue · 3 clips<span style="flex: 1"></span><span style="color: var(--ok)">✓ queued</span></div>
<p style="margin: 0; font-size: 8.5px; line-height: 11.5px; color: var(--tx)">Queued all three. Rendering takes about 14 minutes; I’ll message you here when they’re done.</p>
</div>
</div>
</div>

<div class="mono" style="position: absolute; left: 12px; top: 168px; width: 376px; height: 46px; background: var(--blue); color: var(--on); font-size: 7px">
<div style="height: 15px; display: flex; align-items: center; gap: 6px; padding: 0 7px; border-bottom: 1px solid var(--hair-on); color: var(--on-3)"><span class="eb" style="font-size: 5px; color: var(--on)">Job · running now</span><span style="flex: 1"></span><span>started 11:03 PM</span></div>
<div style="height: 30px; display: flex; align-items: center; gap: 7px; padding: 0 7px">
${spinner('var(--on)', 8)}
<span style="display: flex; flex-direction: column; gap: 3px"><span style="font-weight: 700">render-queue</span><span style="color: var(--on-3)">clip 2 of 3 · frag-clip-02.mp4 · 60 fps</span></span>
<span style="flex: 1"></span>
<span style="display: flex; flex-direction: column; align-items: flex-end; gap: 3px">${frames(fillFrames(16), '9s', 'color: var(--on)')}<span style="color: var(--on-3)">about 6 min left</span></span>
</div>
</div>

${hints([['A', 'Resume'], ['X', 'Branch'], ['Y', 'Rename']], '↕ Browse')}
`;

const row = (src, title, when, opts = {}) => {
  const tag = opts.href ? 'a' : 'button';
  const st = `height: 25px; display: flex; align-items: center; gap: 7px; padding: 0 7px; text-align: left; color: var(--ink); ${opts.first ? '' : 'border-top: 1px solid var(--rule); '}${opts.sel ? 'background: var(--pbrand-soft); outline: 1.5px solid var(--pbrand); outline-offset: -1.5px; position: relative; z-index: 1' : ''}`;
  return `<${tag}${opts.href ? ` href="${opts.href}"` : ' type="button"'} style="${st}">
<span class="mono" style="width: 32px; flex: none; font-size: 5.5px; font-weight: 700; letter-spacing: .06em; color: ${opts.sel ? 'var(--pbrand)' : 'var(--ink-3)'}">${src}</span>
<span style="flex: 1; min-width: 0; font-size: 8.5px; ${opts.sel ? 'font-weight: 700; ' : ''}white-space: nowrap; overflow: hidden; text-overflow: ellipsis">${title}</span>
${opts.live ? `<span style="display: inline-flex; align-items: center; gap: 3px; font-size: 6.5px; color: var(--pbrand)">${spinner('var(--pbrand)', 7)}now</span>` : `<span class="mono" style="font-size: 6.5px; color: var(--ink-3)">${when}</span>`}
</${tag}>`;
};

const filt = (label, n, on = false) => `<button type="button" style="height: 15px; display: inline-flex; align-items: center; gap: 4px; padding: 0 6px; font-size: 7px; ${on ? 'background: var(--ink); color: var(--paper)' : 'background: var(--paper-2); color: var(--ink-2); border: 1px solid var(--rule)'}">${label}<span class="mono" style="font-size: 6px; opacity: .6">${n}</span></button>`;

const bottom = `
<div style="position: absolute; inset: 0; display: flex; flex-direction: column">
${tabs('sessions')}
<div style="flex: 1; padding: 7px 8px 8px; display: flex; flex-direction: column; gap: 6px">
<div style="display: flex; gap: 5px">
<div style="flex: 1; height: 24px; display: flex; align-items: center; gap: 6px; padding: 0 7px; background: var(--paper-2); border: 1px solid var(--ink)"><span style="color: var(--pbrand); display: flex">${icon('search', 11)}</span><span style="font-size: 8.5px; color: var(--ink-3)">Search sessions</span></div>
<button type="button" style="height: 24px; display: flex; align-items: center; gap: 4px; padding: 0 8px; background: var(--pbrand); color: var(--pbrand-on); box-shadow: inset -1px -1px 0 rgba(0,0,0,.45), inset 1px 1px 0 rgba(255,255,255,.45); font-size: 8px; font-weight: 700">${icon('plus', 10)}New</button>
</div>
<div style="display: flex; gap: 4px">${filt('All', 41, true)}${filt('3DS', 12)}${filt('CLI', 23)}${filt('Discord', 6)}</div>
<div style="display: flex; flex-direction: column; background: var(--paper-2); border: 1px solid var(--rule)">
${row('3DS', 'Clipper: export crash on long clips', '', { first: true, live: true, href: 'Chat.dc.html' })}
${row('3DS', 'Morning check-in', '7:30 AM', { href: 'Voice.dc.html' })}
${row('DISCORD', 'TF2 frag video: render queue', 'Yesterday', { sel: true })}
${row('CLI', 'Minecraft mod: config loader', 'Oct 18')}
${row('CLI', 'Wizard101 pet training planner', 'Oct 16')}
</div>
<p class="mono" style="margin: auto 0 0; font-size: 6.5px; color: var(--ink-3)">↓ 36 more · L R to jump by day</p>
</div>
</div>
`;

export default {
  file: 'Sessions.dc.html',
  title: 'Hermes 3DS: Sessions',
  board: '5 · Sessions',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
};
