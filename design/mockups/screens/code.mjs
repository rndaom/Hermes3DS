import { header, hints, icon, kb, wing, btnPrimary, btnSecondary, bracket } from '../lib.mjs';

const K = (s) => `<span style="color: var(--blue); font-weight: 700">${s}</span>`;
const F = (s) => `<span style="font-weight: 700">${s}</span>`;
const C = (s) => `<span style="color: var(--blue)">${s}</span>`;
const S = (s) => `<span style="color: var(--ok)">${s}</span>`;
const N = (s) => `<span style="color: var(--blue)">${s}</span>`;
const I = (n) => '&nbsp;'.repeat(n * 4);

const LINES = [
  [39, ' ', `${K('def')} ${F('render_segments')}(clip, out):`],
  [40, ' ', `${I(1)}${S('"""Encode a clip, one buffer at a time."""')}`],
  [41, ' ', `${I(1)}frames = ${F('load_frames')}(clip)`],
  [42, '-', `${I(1)}buf = ${F('FrameBuffer')}(seconds=${C('MAX_BUFFER_SECONDS')})`],
  [43, '-', `${I(1)}${K('for')} i, frame ${K('in')} ${F('enumerate')}(frames):`],
  [44, '-', `${I(2)}buf[i] = frame`],
  [42, '+', `${I(1)}${K('for')} start ${K('in')} ${F('range')}(${N('0')}, clip.duration, ${C('MAX_BUFFER_SECONDS')}):`],
  [43, '+', `${I(2)}buf = ${F('FrameBuffer')}(seconds=${C('MAX_BUFFER_SECONDS')})`],
  [44, '+', `${I(2)}chunk = frames.${F('slice')}(start, ${C('MAX_BUFFER_SECONDS')})`],
  [45, '+', `${I(2)}${K('for')} i, frame ${K('in')} ${F('enumerate')}(chunk):`],
  [46, '+', `${I(3)}buf[i] = frame`],
  [47, '+', `${I(2)}out.${F('write')}(${F('encode')}(buf))`],
  [48, ' ', `${I(1)}${K('return')} out`],
  [49, ' ', ''],
  [50, ' ', `${K('def')} ${F('encode')}(buf):`],
];

const line = ([n, k, code]) => {
  const bg = k === '+' ? 'background: rgba(0,0,242,.075)' : k === '-' ? 'background: rgba(200,16,28,.06)' : '';
  const mk = k === '+' ? 'color: var(--blue)' : k === '-' ? 'color: var(--bad)' : 'color: transparent';
  return `<div style="height: 11.5px; display: flex; align-items: center; white-space: pre; ${bg}">
<span style="width: 20px; flex: none; text-align: right; padding-right: 5px; color: var(--tx-3)">${n}</span>
<span style="width: 9px; flex: none; font-weight: 700; ${mk}">${k === '-' ? '−' : k === '+' ? '+' : '·'}</span>
<span style="${k === '-' ? 'color: var(--tx-3)' : 'color: var(--tx)'}">${code}</span>
</div>`;
};

const mini = LINES.map(([, k]) => `<span style="display: block; height: 3px; margin-bottom: 1px; width: ${k === ' ' ? 6 : 9}px; background: ${k === '+' ? 'var(--blue)' : k === '-' ? 'var(--bad)' : 'var(--line)'}"></span>`).join('');

const top = `
${header({
  lead: `<span style="display: flex; color: var(--on)">${icon('file', 9)}</span><span class="mono" style="font-size: 7px; font-weight: 700; color: var(--on)">clipper/export.py</span>`,
  title: '<span class="mono" style="font-size: 7px"><span style="color: var(--ok-on)">+6</span> <span style="color: var(--bad-on)">−3</span></span>',
  model: 'python',
  link: '',
})}

<div class="mono" style="position: absolute; left: 0; top: 16px; width: 400px; height: 13px; display: flex; align-items: center; padding: 0 10px; background: var(--scr-2); border-bottom: 1px solid var(--line-b); font-size: 6.5px; color: var(--blue)">@@ render_segments · lines 39–50 @@<span style="flex: 1"></span><span style="color: var(--tx-3)">hunk 1 of 1</span></div>

<div class="mono" style="position: absolute; left: 0; top: 32px; width: 380px; font-size: 7px">
${LINES.map(line).join('\n')}
</div>

<div aria-hidden="true" style="position: absolute; right: 6px; top: 34px; width: 12px; padding: 3px 1px; background: var(--paper-2); border: 1px solid var(--line)">${mini}<span style="position: absolute; left: -1px; right: -1px; top: 10px; height: 34px; border: 1px solid var(--blue)"></span></div>

${hints([['X', 'Wrap'], ['Y', 'Full file'], ['B', 'Back to chat']], '↕ Scroll · L R Files')}
`;

const file = (name, add, del, sel = false, first = false) => `<button type="button" style="height: 22px; display: flex; align-items: center; gap: 6px; padding: 0 7px; text-align: left; color: var(--ink); ${first ? '' : 'border-top: 1px solid var(--rule); '}${sel ? 'background: var(--pbrand-soft); outline: 1.5px solid var(--pbrand); outline-offset: -1.5px; position: relative' : ''}">
<span style="display: flex; color: var(--pbrand)">${icon('file', 10)}</span>
<span class="mono" style="flex: 1; font-size: 7.5px; ${sel ? 'font-weight: 700' : ''}">${name}</span>
<span class="mono" style="font-size: 7px"><span style="color: var(--pok)">+${add}</span> <span style="color: var(--pbad)">−${del}</span></span>
</button>`;

const bottom = `
<div style="position: absolute; inset: 0; padding: 9px 10px 8px; display: flex; flex-direction: column; gap: 6px">
<div style="display: flex; align-items: center; justify-content: space-between">
<a href="Chat.dc.html" class="eb" style="display: inline-flex; align-items: center; gap: 3px; font-size: 5.5px; letter-spacing: .24em; color: var(--ink-3)">${icon('back', 8)}#07 · Review</a>
<span class="mono" style="font-size: 7px; color: var(--ink-3)">2 files · <span style="color: var(--pok)">+9</span> <span style="color: var(--pbad)">−3</span></span>
</div>
<h2 class="disp" style="margin: -1px 0 0; font-size: 25px; color: var(--ink)">Changes</h2>
<div style="display: flex; flex-direction: column; background: var(--paper-2); border: 1px solid var(--rule)">
${file('clipper/export.py', 6, 3, true, true)}
${file('tests/test_export.py', 3, 0)}
</div>
<div style="display: flex; gap: 6px">
<div style="flex: 1; height: 20px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: 1px solid var(--ink); background: var(--paper-2)">
<button type="button" style="background: var(--ink); color: var(--paper); font-size: 7.5px; font-weight: 700">Changes</button>
<button type="button" style="border-left: 1px solid var(--ink); font-size: 7.5px; color: var(--ink-2)">Full file</button>
</div>
<div class="mono" style="height: 20px; display: flex; align-items: center; border: 1px solid var(--ink); background: var(--paper-2); font-size: 7.5px">
<button type="button" style="width: 18px; height: 100%; color: var(--pbrand)">−</button><span style="padding: 0 5px; border-left: 1px solid var(--rule); border-right: 1px solid var(--rule); line-height: 18px">7px</span><button type="button" style="width: 18px; height: 100%; color: var(--pbrand)">+</button>
</div>
</div>
<div style="display: flex; gap: 6px; padding: 5px 7px; background: var(--paper-2); border-left: 2px solid var(--pbrand)">
<span style="display: flex; color: var(--pbrand); padding-top: 1px">${wing(9)}</span>
<p style="margin: 0; font-size: 7.5px; line-height: 10px; color: var(--ink-2)">Long clips now render in 60-second chunks, so the buffer never runs past the frame index. The new test covers a 3-minute clip.</p>
</div>
<div style="margin-top: auto; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px">
<button type="button" style="${btnPrimary(24)}; justify-content: center; padding: 0 4px">${icon('save', 10)}<span style="font-size: 7.5px; font-weight: 700">Save to SD</span></button>
<button type="button" style="${btnSecondary(24)}; justify-content: center; padding: 0 4px">${icon('pc', 10)}<span style="font-size: 7.5px; font-weight: 500">Open on PC</span></button>
<a href="Chat.dc.html" style="${btnSecondary(24)}; justify-content: center; padding: 0 4px">${icon('chat', 10)}<span style="font-size: 7.5px; font-weight: 500">Ask about it</span></a>
</div>
</div>
`;

export default {
  file: 'Code.dc.html',
  title: 'Hermes 3DS: Code review viewer',
  board: '7 · Code review viewer',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
};
