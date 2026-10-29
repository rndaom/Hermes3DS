import { header, hints, spinner, icon, kb, wing, tabs, frames, bounceFrames, meter, ROOT_JS } from '../lib.mjs';

const T1 = 'Found it. ';
const CODE = 'render_segments()';
const T3 = ' puts every clip into a single 60-second buffer, so anything longer runs past the end of the frame index and crashes. The fix is to split long clips into 60-second chunks before rendering. I’ll write that now and re-run the test.';

// Hermes CLI thinking faces and verbs, cycled in step.
const FACES = ['(⌐■_■)', '(◔_◔)', '( •_•)>', '(¬‿¬)', '٩(◕‿◕)۶', '(◕ᴗ◕✿)'];
const VERBS = ['synthesizing', 'pondering', 'reasoning', 'cogitating', 'formulating', 'mulling it over'];

const tool = (name, args, status, time, bad = false) => `<div style="display: flex; align-items: center; gap: 5px; height: 10px; white-space: nowrap">
<span style="color: var(--tx-3)">┊</span>
<span style="font-weight: 500; color: var(--blue)">${name}</span>
<span style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--tx-2)">${args}</span>
<span style="color: ${bad ? 'var(--bad)' : 'var(--ok)'}">${status}</span>
<span style="width: 22px; text-align: right; color: var(--tx-3)">${time}</span>
</div>`;

const top = `
${header({ title: 'Clipper: export crash on long clips', model: 'qwen3.8-27b' })}

<div style="position: absolute; left: 0; top: 16px; width: 400px; height: 193px; padding: 8px 11px 0; display: flex; flex-direction: column; gap: 8px; overflow: hidden">
<div style="align-self: flex-end; max-width: 262px; display: flex; flex-direction: column; align-items: flex-end; gap: 3px">
<span class="eb" style="font-size: 5px; color: var(--tx-3)">You · 9:40 PM</span>
<p style="margin: 0; padding: 5px 7px 6px; background: var(--blue); color: var(--on); font-size: 8.5px; line-height: 11.5px">The export crashes when a clip is longer than 60 seconds. Can you find out why?</p>
</div>

<div style="display: flex; gap: 7px">
<span style="width: 15px; height: 15px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line-b); color: var(--blue)">${wing(9)}</span>
<div style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px">
<span style="display: flex; align-items: center; gap: 5px; height: 15px"><span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--blue)">Hermes</span><span class="mono" style="font-size: 6px; color: var(--tx-3)">9:41 PM</span></span>
<div class="mono" style="display: flex; flex-direction: column; gap: 1px; font-size: 6.5px; padding: 3px 6px; background: var(--scr-2)">
${tool('read_file', 'clipper/export.py', '✓', '0.1s')}
${tool('terminal', 'pytest tests/test_export.py -k long', '✗ 1 failed', '3.2s', true)}
${tool('search', '"MAX_BUFFER_SECONDS"', '✓ 3 hits', '0.2s')}
</div>
<p style="margin: 0; font-size: 9px; line-height: 12.5px; color: var(--tx)">{{t1}}<sc-if value="{{showCode}}" hint-placeholder-val="{{ true }}"><span class="mono" style="font-size: 8px; padding: 0 3px; background: var(--blue-soft); border: 1px solid var(--line-b); color: var(--blue)">${CODE}</span></sc-if>{{t3}}<span class="caret"></span></p>
</div>
</div>
</div>

<div style="position: absolute; left: 0; top: 209px; width: 400px; height: 16px; display: flex; align-items: center; gap: 6px; padding: 0 11px; font-size: 7.5px; color: var(--tx-2); background: var(--scr); border-top: 1px solid var(--line)">
${frames(FACES, '15s', "width: 38px; color: var(--blue); font-family: 'M PLUS 1p', sans-serif", '1.35em')}
${frames(VERBS, '15s', 'width: 62px; font-style: italic', '1.35em')}
<span class="mono" style="font-size: 6.5px; color: var(--tx-3)">18 tok/s · {{tokens}} tok</span>
<span style="flex: 1"></span>
<span class="mono" style="font-size: 6.5px; color: var(--tx-3)">ctx <span style="color: var(--blue)">${meter(.22, 10)}</span> 22%</span>
</div>

${hints([['A', 'Write'], ['Y', 'Hold to talk'], ['B', 'Stop']], '↕ Scroll · L R Tabs')}
`;

const quick = (href, ic, label, sub, k, cls) => `<a href="${href}" style="display: flex; align-items: center; gap: 7px; padding: 0 7px; position: relative; color: var(--ink)${cls}">
<span style="width: 24px; height: 24px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--ink); background: var(--paper); color: var(--pbrand); box-shadow: 1.5px 1.5px 0 var(--pbrand)">${icon(ic, 13)}</span>
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px"><span class="mw" style="font-size: 7.5px; line-height: 1">${label}</span><span style="font-size: 6.5px; color: var(--ink-3); white-space: nowrap">${sub}</span></span>
${k ? kb(k, 'ink') : ''}
</a>`;

const chip = (label, href) => {
  const st = 'height: 15px; display: inline-flex; align-items: center; padding: 0 5px; border: 1px solid var(--rule); background: var(--paper-2); font-size: 7px; color: var(--ink-2)';
  const inner = `<span class="mono" style="color: var(--pbrand)">/</span><span class="mono">${label}</span>`;
  return href ? `<a href="${href}" style="${st}">${inner}</a>` : `<button type="button" style="${st}">${inner}</button>`;
};

const bottom = `
<div style="position: absolute; inset: 0; display: flex; flex-direction: column">
${tabs('chat')}
<div style="flex: 1; padding: 7px 8px 8px; display: flex; flex-direction: column; gap: 6px">

<sc-if value="{{streaming}}" hint-placeholder-val="{{ true }}">
<div style="height: 34px; flex: none; display: flex; align-items: center; gap: 8px; padding: 0 6px 0 9px; background: var(--pbrand); color: var(--pbrand-on)">
${spinner('var(--pbrand-on)', 10)}
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px">
<span style="font-size: 8.5px; font-weight: 700">Hermes is working · step 4 of 5</span>
<span class="mono" style="display: flex; align-items: center; gap: 6px; font-size: 6.5px; color: rgba(255,255,255,.75)">▰▰▰▰▱<span>writing the reply</span>${frames(bounceFrames(6, 2), '1.2s')}</span>
</span>
<button type="button" style="height: 22px; display: inline-flex; align-items: center; gap: 5px; padding: 0 6px; background: var(--paper-2); color: var(--pbrand); box-shadow: inset -1px -1px 0 rgba(0,0,0,.25); font-size: 8px; font-weight: 700">${icon('stop', 8)}Stop${kb('B', 'blue')}</button>
</div>
</sc-if>
<sc-if value="{{done}}" hint-placeholder-val="{{ false }}">
<a href="Approval.dc.html" style="height: 34px; flex: none; display: flex; align-items: center; gap: 8px; padding: 0 7px 0 0; background: var(--accent); color: var(--accent-ink); overflow: hidden">
<span class="mono" aria-hidden="true" style="align-self: stretch; width: 14px; display: flex; align-items: center; justify-content: center; background: var(--accent-ink); color: var(--accent); font-size: 9px; font-weight: 700">!</span>
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px">
<span style="font-size: 8.5px; font-weight: 700">Hermes needs your OK</span>
<span class="mono" style="font-size: 6.5px">Remove-Item -Recurse .\\build\\</span>
</span>
<span class="cp" style="font-size: 7px">Review</span>${kb('A', 'ink')}
</a>
</sc-if>

<div style="height: 32px; flex: none; display: flex; align-items: center; gap: 7px; padding: 0 7px 0 8px; background: var(--paper-2); border: 1px solid var(--ink); box-shadow: 2px 2px 0 var(--pbrand)">
<span style="color: var(--pbrand); display: flex">${icon('keyboard', 12)}</span>
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px">
<span style="font-size: 9px; color: var(--ink-3)">Write to Hermes…<span class="caret" style="background: var(--pbrand); height: 8px; width: 4px"></span></span>
<span style="font-size: 6.5px; color: var(--ink-3)">Queues until this step ends</span>
</span>
${kb('A', 'ink')}
</div>

<div style="flex: 1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: repeat(2, minmax(0, 1fr)); background: var(--paper-2); border: 1px solid var(--rule)">
${quick('Voice.dc.html', 'mic', 'Talk', 'Hold to speak', 'Y', '')}
${quick('Media.dc.html', 'camera', 'Photo', 'Outer camera', '', '; border-left: 1px solid var(--rule)')}
${quick('Media.dc.html', 'clip', 'Attach', 'From the SD card', '', '; border-top: 1px solid var(--rule)')}
${quick('Commands.dc.html', 'slash', 'Commands', '24 available', '', '; border-top: 1px solid var(--rule); border-left: 1px solid var(--rule)')}
</div>

<div style="display: flex; gap: 4px">
${chip('new')}${chip('model', 'Commands.dc.html')}${chip('retry')}${chip('undo')}${chip('steer')}
</div>
</div>
</div>
`;

const script = `class Component extends DCLogic {
static T1 = ${JSON.stringify(T1)};
static CODE = ${JSON.stringify(CODE)};
static T3 = ${JSON.stringify(T3)};
state = { n: 0 };
componentDidMount() {
const C = this.constructor;
const full = C.T1.length + C.CODE.length + C.T3.length;
this.timer = setInterval(() => {
const n = (this.state.n || 0) + 2;
this.setState({ n: n > full + 170 ? 0 : n });
}, 42);
}
componentWillUnmount() { clearInterval(this.timer); }
renderVals() {
${ROOT_JS}
const C = this.constructor;
const a = C.T1.length, b = a + C.CODE.length, full = b + C.T3.length;
const n = this.props.motion === false ? full : Math.min((this.state && this.state.n) || 0, full);
return {
rootClass,
t1: C.T1.slice(0, n),
showCode: n >= a + 2,
t3: n > b ? C.T3.slice(0, n - b) : '',
tokens: String(96 + Math.floor(n / 3.4)),
streaming: n < full,
done: n >= full,
};
}
}`;

export default {
  file: 'Chat.dc.html',
  title: 'Hermes 3DS: Chat, streaming',
  board: '2 · Chat, streaming',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
  script,
  preview: { t1: T1, showCode: true, t3: T3.slice(0, T3.indexOf(' I’ll') + 35), tokens: '162', streaming: true, done: false },
};
