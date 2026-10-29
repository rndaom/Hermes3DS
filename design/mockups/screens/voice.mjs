import { header, hints, icon, kb, wing, frames, blockFrames, rng, ROOT_JS } from '../lib.mjs';

// Live mic level as a segmented ASCII meter, redrawn every frame like a terminal VU.
// Each column is a bar of ▄ cells mirrored around the centre row.
const COLS = 44, ROWS = 9, MID = 4, FR = 14;
const r = rng(31);
const wave = [];
for (let f = 0; f < FR; f++) {
  const amp = [];
  for (let c = 0; c < COLS; c++) {
    const env = Math.sin(Math.PI * (c + .5) / COLS) ** .8;                          // louder in the middle
    const syl = .5 + .5 * Math.abs(Math.sin(f * .9 + c * .38) * Math.cos(f * .45 - c * .17)); // syllables rolling through
    amp.push(Math.round(MID * env * syl * (.6 + r() * .4)));
  }
  wave.push(Array.from({ length: ROWS }, (_, row) => amp.map((a) => (Math.abs(row - MID) <= a ? '▄' : '·')).join(' ')));
}

const top = `
${header({ title: 'Morning check-in', model: 'qwen3.8-27b' })}

<div style="position: absolute; left: 11px; top: 23px; width: 378px; display: flex; gap: 7px">
<span style="width: 15px; height: 15px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line-b); color: var(--blue)">${wing(9)}</span>
<div style="flex: 1; display: flex; flex-direction: column; gap: 3px">
<span style="display: flex; align-items: center; gap: 5px; height: 15px"><span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--blue)">Hermes</span><span style="display: inline-flex; align-items: center; gap: 3px; font-size: 6.5px; color: var(--tx-3)">${icon('speaker', 8)}spoken · 7:30 AM</span></span>
<p style="margin: 0; font-size: 8.5px; line-height: 11.5px; color: var(--tx-2)">Good morning. Two scheduled jobs ran overnight: the Clipper backup finished, and the TF2 render queue has 3 clips waiting for you.</p>
</div>
</div>

<div style="position: absolute; left: 12px; top: 78px; width: 376px; height: 1px; background: var(--line)"></div>

<div style="position: absolute; left: 12px; top: 88px; width: 376px; display: flex; align-items: flex-end; justify-content: space-between">
<div style="display: flex; flex-direction: column; gap: 5px">
<span class="eb" style="display: inline-flex; align-items: center; gap: 5px; font-size: 5.5px; letter-spacing: .3em; color: var(--blue)"><span class="live" style="width: 5px; height: 5px; border-radius: 3px; background: var(--blue)"></span>Recording</span>
<span class="disp" style="font-size: 34px; color: var(--tx)">Listening</span>
</div>
<span class="mono" style="font-size: 22px; line-height: 1; font-weight: 500; color: var(--blue); letter-spacing: -.02em">{{clock}}<span style="font-size: 9px; color: var(--tx-3)"> / 2:00</span></span>
</div>

<div class="ascii" aria-hidden="true" style="position: absolute; left: 12px; top: 130px; width: 376px; font-size: 7px; color: var(--blue)">${blockFrames(wave, 6, '1.75s')}</div>

<p style="position: absolute; left: 12px; top: 194px; margin: 0; display: flex; align-items: center; gap: 6px; font-size: 8px; color: var(--tx-2)"><span class="ka" style="color: var(--blue)">(◕ᴗ◕✿)</span>Your PC turns this into text when you let go</p>

${hints([['Y', 'Release to send', 'hot'], ['B', 'Cancel']], 'Speech-to-text on RANDOM-PC')}
`;

const bottom = `
<div style="position: absolute; inset: 0; padding: 9px 10px 8px; display: flex; flex-direction: column; gap: 7px">
<div style="display: flex; align-items: center; justify-content: space-between">
<a href="Chat.dc.html" class="eb" style="display: inline-flex; align-items: center; gap: 3px; font-size: 5.5px; letter-spacing: .24em; color: var(--ink-3)">${icon('back', 8)}#04 · Talk</a>
<span class="mono" style="font-size: 7px; color: var(--pbrand)">● REC {{clock}}</span>
</div>

<div style="flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; background: var(--paper-2); border: 1px solid var(--ink); box-shadow: 3px 3px 0 var(--pbrand)">
<a href="Chat.dc.html" aria-label="Release to send" style="width: 74px; height: 74px; display: flex; align-items: center; justify-content: center; border-radius: 37px; background: var(--pbrand); color: var(--pbrand-on); box-shadow: 0 0 0 5px var(--pbrand-soft), 0 0 0 6px var(--pbrand), inset -2px -2px 0 rgba(0,0,0,.3), inset 2px 2px 0 rgba(255,255,255,.35)">${icon('mic', 28)}</a>
<span style="font-size: 9px; font-weight: 700; color: var(--ink)">Hold Y or keep your finger here</span>
<span class="mono" style="display: flex; align-items: center; gap: 5px; font-size: 7px; color: var(--ink-3)">level ${frames(['▁▃▅▆▇', '▂▄▆▇█', '▁▂▄▅▆', '▂▃▅▇▇', '▁▂▃▄▅', '▃▅▆▇█'], '1s', 'color: var(--pbrand); letter-spacing: 1px')}</span>
</div>

<button type="button" style="height: 30px; display: flex; align-items: center; gap: 8px; padding: 0 8px; background: var(--paper-2); border: 1px solid var(--rule); text-align: left">
<span style="color: var(--pbrand); display: flex">${icon('speaker', 13)}</span>
<span style="flex: 1; display: flex; flex-direction: column; gap: 3px"><span style="font-size: 8.5px; font-weight: 700; color: var(--ink)">Speak replies aloud</span><span style="font-size: 6.5px; color: var(--ink-3)">Hermes answers through the 3DS speakers</span></span>
<span aria-hidden="true" style="width: 24px; height: 13px; padding: 2px; display: flex; justify-content: flex-end; background: var(--pbrand)"><span style="width: 9px; height: 9px; background: var(--pbrand-on)"></span></span>
</button>

<p style="margin: 0; font-size: 6.5px; color: var(--ink-3)">Speech-to-text runs on RANDOM-PC · nothing is stored on the 3DS</p>
</div>
`;

const script = `class Component extends DCLogic {
state = { s: 4 };
componentDidMount() { this.tick = setInterval(() => this.setState({ s: this.state.s >= 119 ? 0 : this.state.s + 1 }), 1000); }
componentWillUnmount() { clearInterval(this.tick); }
renderVals() {
${ROOT_JS}
const s = (this.state && this.state.s) || 0;
return { rootClass, clock: Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') };
}
}`;

export default {
  file: 'Voice.dc.html',
  title: 'Hermes 3DS: Hold Y to talk',
  board: '4 · Hold Y to talk',
  device: { hot: ['Y'], notif: 'on' },
  top,
  bottom,
  script,
  preview: { clock: '0:04' },
};
