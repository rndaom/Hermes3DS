import { header, hints, icon, kb, wing, frames, bounceFrames, BEVEL, BEVEL_SOFT, ROOT_JS } from '../lib.mjs';

const top = `
${header({ title: 'Clipper: export crash on long clips', model: 'qwen3.8-27b' })}

<div style="position: absolute; left: 11px; top: 22px; width: 378px; display: flex; gap: 7px; opacity: .62">
<span style="width: 15px; height: 15px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line-b); color: var(--blue)">${wing(9)}</span>
<p style="margin: 1px 0 0; font-size: 8px; line-height: 11px; color: var(--tx-2); height: 22px; overflow: hidden">Found it. <span class="mono" style="font-size: 7px; color: var(--blue)">render_segments()</span> puts every clip into a single 60-second buffer. The fix is to split long clips into 60-second chunks. First I’ll clear the old build output so the test runs clean.</p>
</div>

<section aria-label="Approval needed" style="position: absolute; left: 12px; top: 54px; width: 373px; height: 150px; background: var(--paper-2); border: 1px solid var(--tx); box-shadow: 3px 3px 0 var(--blue)">
<div style="height: 15px; display: flex; align-items: center; gap: 6px; padding: 0 7px; background: var(--accent); color: var(--accent-ink); border-bottom: 1px solid var(--tx)">
<span class="mono live" aria-hidden="true" style="font-size: 7px; font-weight: 700">▲</span>
<span class="eb" style="font-size: 5.5px; letter-spacing: .22em">Approval needed</span>
<span class="mono" aria-hidden="true" style="flex: 1; overflow: hidden; white-space: nowrap; font-size: 7px; opacity: .35">${'/'.repeat(60)}</span>
<span class="mono" style="font-size: 6.5px">terminal</span>
<span class="mono" style="font-size: 6.5px; font-weight: 700">waiting {{elapsed}}</span>
</div>
<div style="padding: 8px 9px 0; display: flex; flex-direction: column; gap: 7px">
<h2 class="disp" style="margin: 0; font-size: 22px; color: var(--tx)">Hermes wants to delete a folder</h2>
<div class="mono" style="padding: 5px 7px; background: var(--blue); color: var(--on); font-size: 7.5px; line-height: 11px">
<div><span style="color: var(--on-3)">PS&gt;</span> Remove-Item -Recurse -Force .\\build\\</div>
<div style="color: var(--on-3); font-size: 6.5px">in ~/projects/demo</div>
</div>
<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line)">
<div style="padding: 4px 0; display: flex; align-items: center; gap: 5px; font-size: 7.5px; color: var(--bad)">${icon('warn', 10)}<span><b>Flagged:</b> recursive delete</span></div>
<div style="padding: 4px 0 4px 8px; border-left: 1px solid var(--line); display: flex; align-items: center; gap: 5px; font-size: 7.5px; color: var(--tx-2)">${icon('file', 10)}<span>Affects <b style="color: var(--tx)">1,284 files</b></span></div>
</div>
<p style="margin: 0; font-size: 7.5px; line-height: 10px; color: var(--tx-2)"><span style="color: var(--blue)">☤ Why:</span> “Clearing stale build output so the re-test starts clean.”</p>
</div>
</section>

${hints([['A', 'Allow once', 'hot'], ['B', 'Deny']], '', `<span style="display: inline-flex; align-items: center; gap: 4px; color: var(--on-2)"><span class="live" style="width: 5px; height: 5px; border-radius: 3px; background: var(--accent)"></span>Light stays on until you answer</span>`)}
`;

const choice = (on, title, sub, k, primary = false, bad = false) => `<button type="button" onClick="{{${on}}}" style="height: 36px; display: flex; align-items: center; gap: 8px; padding: 0 8px; text-align: left; ${primary
  ? `background: var(--pbrand); color: var(--pbrand-on); box-shadow: ${BEVEL}`
  : bad
    ? 'background: var(--paper-2); color: var(--pbad); border: 1px solid var(--pbad)'
    : `background: var(--paper-2); color: var(--ink); border: 1px solid var(--rule); box-shadow: ${BEVEL_SOFT}`}">
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px"><span style="font-size: 9px; font-weight: 700">${title}</span>${sub ? `<span style="font-size: 6.5px; opacity: .75">${sub}</span>` : ''}</span>
${k ? kb(k, primary ? '' : bad ? 'ink' : 'ink') : `<span class="mono" style="font-size: 8px; opacity: .5">›</span>`}
</button>`;

const bottom = `
<div style="position: absolute; inset: 0; padding: 9px 10px 8px; display: flex; flex-direction: column; gap: 6px">
<div style="display: flex; align-items: center; justify-content: space-between">
<span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--ink-3)">#03 · Approve</span>
<span class="mono" style="font-size: 7px; color: var(--ink-3)">waiting {{elapsed}}</span>
</div>
<h2 class="disp" style="margin: -1px 0 3px; font-size: 25px; color: var(--ink)">Run this command?</h2>

<sc-if value="{{undecided}}" hint-placeholder-val="{{ true }}">
<div style="display: flex; flex-direction: column; gap: 6px">
${choice('allowOnce', 'Allow once', 'Just this command, right now', 'A', true)}
<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px">
${choice('allowSession', 'This session', 'Until you start a new one')}
${choice('allowAlways', 'Always', 'For commands like this')}
</div>
${choice('deny', 'Deny', 'Hermes will find another way', 'B', false, true)}
</div>
</sc-if>

<sc-if value="{{decided}}" hint-placeholder-val="{{ false }}">
<div style="height: 125px; display: flex; flex-direction: column; justify-content: center; gap: 6px; padding: 0 12px; background: var(--paper-2); border: 1px solid var(--ink); box-shadow: 3px 3px 0 var(--pbrand)">
<span class="eb" style="font-size: 5.5px; color: var(--pbrand)">{{resultTag}}</span>
<span style="font-size: 12px; font-weight: 700; color: var(--ink)">{{resultTitle}}</span>
<span class="mono" style="display: flex; align-items: center; gap: 6px; font-size: 7px; color: var(--ink-3)">${frames(bounceFrames(8, 2), '1.2s', 'color: var(--pbrand)')}{{resultSub}}</span>
</div>
</sc-if>

<p style="margin: auto 0 0; display: flex; align-items: center; gap: 5px; font-size: 7px; color: var(--ink-3)">${icon('help', 9)}Approval mode: manual · change it in More › Settings</p>
</div>
`;

const script = `class Component extends DCLogic {
state = { s: 12, decision: '' };
componentDidMount() {
this.tick = setInterval(() => this.setState({ s: this.state.decision ? this.state.s : this.state.s + 1 }), 1000);
}
componentWillUnmount() { clearInterval(this.tick); clearTimeout(this.reset); }
decide(d) {
this.setState({ decision: d });
clearTimeout(this.reset);
this.reset = setTimeout(() => this.setState({ decision: '', s: 12 }), 2800);
}
renderVals() {
${ROOT_JS}
const s = (this.state && this.state.s) || 12;
const d = (this.state && this.state.decision) || '';
const R = {
once: ['Allowed once', 'Running it now', 'Remove-Item finished · back to the test'],
session: ['Allowed for this session', 'Running it now', 'Won\\u2019t ask again until a new session'],
always: ['Always allowed', 'Running it now', 'Saved to approvals · change it in Settings'],
deny: ['Denied', 'Hermes is told no', 'It will find another way'],
}[d] || ['', '', ''];
return {
rootClass,
elapsed: Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'),
undecided: !d,
decided: !!d,
resultTitle: R[0],
resultTag: R[1],
resultSub: R[2],
allowOnce: () => this.decide('once'),
allowSession: () => this.decide('session'),
allowAlways: () => this.decide('always'),
deny: () => this.decide('deny'),
};
}
}`;

export default {
  file: 'Approval.dc.html',
  title: 'Hermes 3DS: Approval request',
  board: '3 · Approval request',
  device: { hot: ['A'], notif: 'blink' },
  top,
  bottom,
  script,
  interactive: true,
  preview: { elapsed: '0:12', undecided: true, decided: false },
};
