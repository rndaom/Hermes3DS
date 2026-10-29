// Shared pieces for the Hermes 3DS mockup artboards.
// Every artboard is one .dc.html file: device shell + top screen (400x240) + bottom screen (320x240),
// drawn at 2x. Screens are authored at native 3DS pixels inside a `zoom: 2` box.
//
// Look: Nous blue and white. Both screens are white with dark ink; electric blue is the structure
// (header/footer bands, terminal panels, ASCII art, your own messages). Yellow only for "needs you now". Motion is text: braille spinners, ASCII bars, streaming, waveforms.
// Nothing moves unless it reports real state.

export const BLOB = {
  mondwest: '/_blob/7709a9fb029592ebf8fdd8e026dc98ff',
  rulesExp400: '/_blob/dc7c299f9f7e36cde64bb13213377f39',
  rulesExp700: '/_blob/6e7355800927a058b1bf3b4bacc7e03a',
  rulesCmp400: '/_blob/6f23c90ac8f3b16e0901d3c8b9534bb0',
  rulesCmp500: '/_blob/54ef63aecf5606dbf49d80be58a205f5',
  art: '/_blob/ff1ecf9f39431df1c472da6bdfcbe73e',
};

// Local equivalents, used only by the preview pages (served from design/ on :8765).
export const LOCAL = {
  [BLOB.mondwest]: '/fonts/Mondwest-Regular.woff2',
  [BLOB.rulesExp400]: '/fonts/RulesExpanded-Regular.woff2',
  [BLOB.rulesExp700]: '/fonts/RulesExpanded-Bold.woff2',
  [BLOB.rulesCmp400]: '/fonts/RulesCompressed-Regular.woff2',
  [BLOB.rulesCmp500]: '/fonts/RulesCompressed-Medium.woff2',
  [BLOB.art]: '/art/filler-bg0.webp',
};

export const ART = BLOB.art;

// Geist Mono has the block, shade and box-drawing glyphs at full mono width, so ASCII art lines up.
const GFONTS = 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&amp;family=Geist+Mono:wght@400;500;700&amp;family=Courier+Prime:wght@700&amp;family=M+PLUS+1p:wght@400;700&amp;display=swap';

// ---------------------------------------------------------------- palette
const TOKENS = {
  // device shell
  'shell-hi': '#3434ff', shell: '#0000f2', 'shell-lo': '#0000c0', 'shell-deep': '#000070', 'shell-ink': 'rgba(242,242,255,.62)',
  btn: '#0a0a2a', 'btn-hi': '#2a2a5c', 'btn-ink': '#a9a9e6',
  // top screen: white field, ink text, blue structure
  scr: '#fbfbfe', 'scr-2': '#ececfc',
  tx: '#0d0d24', 'tx-2': '#3a3a63', 'tx-3': '#66668e',
  blue: '#0000f2', 'blue-soft': 'rgba(0,0,242,.07)', line: 'rgba(13,13,36,.13)', 'line-b': 'rgba(0,0,242,.30)',
  ok: '#0b7a3a', bad: '#c8101c',
  // text on blue surfaces (bands, terminal panels, inverse chips)
  on: '#ffffff', 'on-2': 'rgba(255,255,255,.80)', 'on-3': 'rgba(255,255,255,.58)', 'hair-on': 'rgba(255,255,255,.24)',
  'ok-on': '#8dffb8', 'bad-on': '#ffc2c7',
  // "needs you now"
  accent: '#f2f200', 'accent-ink': '#0d0d24',
  // bottom screen: white deck, blue
  paper: '#f2f2f4', 'paper-2': '#ffffff', 'paper-3': '#e2e2ee',
  ink: '#0d0d24', 'ink-2': '#43436a', 'ink-3': '#5f5f88',
  rule: 'rgba(13,13,36,.16)', 'rule-2': 'rgba(13,13,36,.09)',
  pbrand: '#0000f2', 'pbrand-on': '#ffffff', 'pbrand-soft': 'rgba(0,0,242,.07)', pbad: '#c8101c', pok: '#0b7a3a',
};
const vars = (o) => Object.entries(o).map(([k, v]) => `--${k}:${v}`).join(';');

// ---------------------------------------------------------------- helmet CSS
export const CSS = `
@font-face{font-family:'Mondwest';src:url('${BLOB.mondwest}') format('woff2')}
@font-face{font-family:'Rules Expanded';font-weight:400;src:url('${BLOB.rulesExp400}') format('woff2')}
@font-face{font-family:'Rules Expanded';font-weight:700;src:url('${BLOB.rulesExp700}') format('woff2')}
@font-face{font-family:'Rules Compressed';font-weight:400;src:url('${BLOB.rulesCmp400}') format('woff2')}
@font-face{font-family:'Rules Compressed';font-weight:500;src:url('${BLOB.rulesCmp500}') format('woff2')}
body{margin:0}
a{color:inherit;text-decoration:none}
button{font:inherit;color:inherit;margin:0;padding:0;border:0;background:none;cursor:pointer;text-align:inherit}
.hx{${vars(TOKENS)};-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.hx *{box-sizing:border-box}
.kb{display:inline-flex;align-items:center;justify-content:center;flex:none;width:10px;height:10px;border-radius:5px;background:var(--on);color:var(--blue);font-family:'Rules Expanded',sans-serif;font-weight:700;font-size:5.5px;line-height:1}
.kb.hot{background:var(--accent);color:var(--accent-ink)}
.kb.sq{width:auto;min-width:10px;padding:0 2.5px;border-radius:2px}
.kb.ink{background:var(--ink);color:var(--paper)}
.kb.blue{background:var(--pbrand);color:var(--pbrand-on)}
.eb{font-family:'Rules Expanded',sans-serif;font-weight:700;text-transform:uppercase;letter-spacing:.14em;line-height:1}
.mono{font-family:'Geist Mono',monospace}
.cp{font-family:'Courier Prime','Geist Mono',monospace;font-weight:700;text-transform:uppercase;letter-spacing:.16em}
.disp{font-family:'Rules Compressed','Rules Expanded',sans-serif;font-weight:400;line-height:.9;letter-spacing:-.004em}
.mw{font-family:'Mondwest',serif;text-transform:uppercase;letter-spacing:.1em}
.ka{font-family:'M PLUS 1p','Segoe UI Symbol',sans-serif}
.ascii{font-family:'Geist Mono',monospace;white-space:pre;line-height:1;letter-spacing:0;font-variant-ligatures:none}
.dots{background-image:radial-gradient(circle at 1px 1px,var(--rule-2) .7px,transparent .9px);background-size:6px 6px}
.dots-b{background-image:radial-gradient(circle at 1px 1px,rgba(0,0,242,.10) .7px,transparent .9px);background-size:6px 6px}
.glass{position:absolute;inset:0;pointer-events:none;background:linear-gradient(116deg,rgba(255,255,255,.10) 0%,rgba(255,255,255,.03) 24%,rgba(255,255,255,0) 38%)}
.glass.matte{background:linear-gradient(116deg,rgba(255,255,255,.18) 0%,rgba(255,255,255,0) 30%)}
.clip{display:inline-block;overflow:hidden;height:var(--lh,1em);line-height:var(--lh,1em);vertical-align:middle}
.strip{display:block;animation:hx-strip var(--t,.8s) steps(var(--n,10)) var(--dl,0s) infinite}
.strip > span{display:block;height:var(--lh,1em);line-height:var(--lh,1em);white-space:pre}
.caret{display:inline-block;width:4.5px;height:9px;margin-left:1px;vertical-align:-1px;background:var(--blue);animation:hx-blink 1s step-end infinite}
.live{animation:hx-blink 1.2s step-end infinite}
@keyframes hx-strip{from{transform:translateY(0)}to{transform:translateY(calc(var(--n,10) * -1 * var(--lh,1em)))}}
@keyframes hx-blink{0%,49%{opacity:1}50%,100%{opacity:0}}
.still *,.still *::before,.still *::after{animation:none !important}
@media (prefers-reduced-motion: reduce){.hx *,.hx *::before,.hx *::after{animation:none !important}}
`.trim();

// ---------------------------------------------------------------- icons
const ICON = {
  pc: '<rect x="3" y="4" width="18" height="12"></rect><path d="M8 20h8M12 16v4"></path>',
  scan: '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"></path><path d="M8 12h8"></path>',
  keyboard: '<rect x="3" y="6" width="18" height="12"></rect><path d="M7 10h1M11 10h1M15 10h1M7 14h10"></path>',
  help: '<circle cx="12" cy="12" r="9"></circle><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .8-1 1.5V14M12 17v.6"></path>',
  chat: '<path d="M4 5h16v11H9l-5 4z"></path>',
  sessions: '<rect x="4" y="4" width="16" height="6"></rect><rect x="4" y="14" width="16" height="6"></rect>',
  skills: '<path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"></path>',
  jobs: '<circle cx="12" cy="12" r="8"></circle><path d="M12 8v4l3 2"></path>',
  more: '<path d="M5 12h2M11 12h2M17 12h2" stroke-width="3"></path>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3"></path>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"></path><circle cx="12" cy="13" r="3.5"></circle>',
  clip: '<path d="M20 11l-8.5 8.5a5 5 0 0 1-7-7L13 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L14 7"></path>',
  slash: '<rect x="3" y="3" width="18" height="18"></rect><path d="M14.5 7l-5 10"></path>',
  stop: '<rect x="6" y="6" width="12" height="12" fill="currentColor" stroke="none"></rect>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"></path>',
  x: '<path d="M6 6l12 12M18 6L6 18"></path>',
  warn: '<path d="M12 4l9 16H3z"></path><path d="M12 10v4M12 17v.6"></path>',
  file: '<path d="M6 3h8l4 4v14H6z"></path><path d="M14 3v4h4"></path>',
  image: '<rect x="3" y="5" width="18" height="14"></rect><circle cx="9" cy="10" r="1.8"></circle><path d="M21 16l-5-5-8 8"></path>',
  save: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"></path>',
  search: '<circle cx="11" cy="11" r="6"></circle><path d="M20 20l-4.5-4.5"></path>',
  plus: '<path d="M12 5v14M5 12h14"></path>',
  speaker: '<path d="M4 9h4l5-4v14l-5-4H4z"></path><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"></path>',
  back: '<path d="M15 5l-7 7 7 7"></path>',
  play: '<path d="M7 5l12 7-12 7z" fill="currentColor"></path>',
  power: '<path d="M12 3v8"></path><path d="M7 6.5a7 7 0 1 0 10 0"></path>',
  home: '<path d="M4 11l8-7 8 7v9H4z"></path><path d="M10 20v-5h4v5"></path>',
  branch: '<circle cx="6" cy="5" r="2"></circle><circle cx="6" cy="19" r="2"></circle><circle cx="18" cy="8" r="2"></circle><path d="M6 7v10M18 10c0 4-6 3-11 7"></path>',
  terminal: '<rect x="3" y="4" width="18" height="16"></rect><path d="M7 9l3 3-3 3M12 15h5"></path>',
  diff: '<path d="M8 3v8M4 7h8M4 17h8M16 13v8"></path>',
  pin: '<path d="M9 3h6l-1 6 4 4H6l4-4z"></path><path d="M12 13v8"></path>',
};

export function icon(name, size = 12, style = '') {
  return `<svg aria-hidden="true" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter"${style ? ` style="${style}"` : ''}>${ICON[name]}</svg>`;
}

// Hermes wing: an original single wing with three feather tips, the app's small mark.
export function wing(size = 10, color = 'currentColor', style = '') {
  return `<svg aria-hidden="true" viewBox="0 0 24 24" width="${size}" height="${size}" fill="${color}"${style ? ` style="${style}"` : ''}><path d="M2 19.5C5.6 11 12.6 5.2 22.6 3c-1.9 2.6-3.8 4.2-6 5.3 2.1-.1 3.9.1 5.5.6-2.6 2.5-5.6 3.9-8.7 4.5 1.8.4 3.4 1 4.7 1.8C13.3 18.1 7.9 20 2 19.5z"></path></svg>`;
}

export const kb = (k, cls = '') => `<span class="kb${cls ? ' ' + cls : ''}">${k}</span>`;

// Deterministic randomness, so rebuilds are stable.
export function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------------------------------------------------------------- ASCII
// ANSI Shadow block letters, like the Hermes CLI banner.
const SHADOW = {
  H: ['██╗  ██╗', '██║  ██║', '███████║', '██╔══██║', '██║  ██║', '╚═╝  ╚═╝'],
  E: ['███████╗', '██╔════╝', '█████╗  ', '██╔══╝  ', '███████╗', '╚══════╝'],
  R: ['██████╗ ', '██╔══██╗', '██████╔╝', '██╔══██╗', '██║  ██║', '╚═╝  ╚═╝'],
  M: ['███╗   ███╗', '████╗ ████║', '██╔████╔██║', '██║╚██╔╝██║', '██║ ╚═╝ ██║', '╚═╝     ╚═╝'],
  S: ['███████╗', '██╔════╝', '███████╗', '╚════██║', '███████║', '╚══════╝'],
};
export const shadowText = (word) => [0, 1, 2, 3, 4, 5].map((r) => [...word].map((c) => SHADOW[c][r]).join('')).join('\n');

// Frame animation: a column of text frames stepped like a terminal redraw.
export function frames(list, dur, style = '', lh = '1.25em', delay = '') {
  return `<span class="clip" aria-hidden="true" style="--lh: ${lh}${style ? '; ' + style : ''}"><span class="strip" style="--n: ${list.length}; --t: ${dur}${delay ? '; --dl: ' + delay : ''}">${list.map((x) => `<span>${esc(x)}</span>`).join('')}</span></span>`;
}

// Multi-line frames (each frame is an array of lines), e.g. an ASCII level meter.
export function blockFrames(list, lh, dur, style = '') {
  const rows = list[0].length;
  return `<span class="clip" aria-hidden="true" style="--lh: ${rows * lh}px; display: block${style ? '; ' + style : ''}"><span class="strip" style="--n: ${list.length}; --t: ${dur}">${list.map((f) => `<span style="line-height: ${lh}px">${esc(f.join(String.fromCharCode(10)))}</span>`).join('')}</span></span>`;
}

export const BRAILLE = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'.split('');
export function spinner(color = 'currentColor', size = 8) {
  return frames(BRAILLE, '.8s', `font-family: 'Geist Mono', 'Segoe UI Symbol', monospace; font-size: ${size}px; color: ${color}; width: 1ch`, '1em');
}

// [■■■       ] bouncing back and forth, for "working, no ETA".
export function bounceFrames(width = 10, len = 3, on = '■', off = ' ') {
  const out = [];
  const make = (p) => '[' + Array.from({ length: width }, (_, i) => (i >= p && i < p + len ? on : off)).join('') + ']';
  for (let p = 0; p <= width - len; p++) out.push(make(p));
  for (let p = width - len - 1; p > 0; p--) out.push(make(p));
  return out;
}

// ▰▰▰▱▱ filling up, for "working, with progress".
export const fillFrames = (width = 10, on = '█', off = '░') => Array.from({ length: width + 1 }, (_, i) => on.repeat(i) + off.repeat(width - i));

// A static ASCII meter.
export const meter = (frac, width = 10, on = '█', off = '░') => on.repeat(Math.round(frac * width)) + off.repeat(width - Math.round(frac * width));

// ---------------------------------------------------------------- top-screen chrome
const SIGNAL = `<svg aria-hidden="true" viewBox="0 0 9 7" width="9" height="7" fill="currentColor" shape-rendering="crispEdges"><rect x="0" y="5" width="2" height="2"></rect><rect x="3.5" y="3" width="2" height="4"></rect><rect x="7" y="0" width="2" height="7"></rect></svg>`;

export const field = () => `<div class="dots-b" aria-hidden="true" style="position: absolute; inset: 0"></div>`;

export function header({ title = '', model = '', link = 'LAN', lead = '' } = {}) {
  return `<header style="position: absolute; left: 0; top: 0; width: 400px; height: 16px; display: flex; align-items: center; gap: 6px; padding: 0 7px; background: var(--blue); color: var(--on); z-index: 6">
${lead || `${wing(9, 'var(--on)')}<span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--on)">Hermes</span>`}
<span aria-hidden="true" style="width: 1px; height: 7px; background: var(--hair-on)"></span>
<span style="flex: 1; min-width: 0; font-size: 7.5px; color: var(--on-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis">${title}</span>
${model ? `<span class="mono" style="font-size: 6.5px; line-height: 9px; color: var(--on); padding: 0 4px; border: 1px solid var(--hair-on)">${model}</span>` : ''}
${link ? `<span style="display: inline-flex; align-items: center; gap: 3px; color: var(--on)">${SIGNAL}<span class="eb" style="font-size: 5px">${link}</span></span>` : ''}
</header>`;
}

// hints: [['A','Write'], ['Y','Release to send','hot'], ...]
export function hints(items, right = '', rightHtml = '') {
  const cells = items.map(([k, label, cls]) => `<span style="display: inline-flex; align-items: center; gap: 3px">${kb(k, cls || (k.length > 1 ? 'sq' : ''))}${label}</span>`).join('\n');
  return `<footer style="position: absolute; left: 0; bottom: 0; width: 400px; height: 15px; display: flex; align-items: center; gap: 9px; padding: 0 7px; background: var(--blue); font-size: 7px; color: var(--on); z-index: 6">
${cells}
<span style="flex: 1"></span>
${rightHtml || `<span style="color: var(--on-3)">${right}</span>`}
</footer>`;
}

// Bottom-screen tab bar. active: 'chat' | 'sessions' | 'skills' | 'jobs' | 'more'
export function tabs(active) {
  const T = [
    ['chat', 'Chat', 'Chat.dc.html'],
    ['sessions', 'Sessions', 'Sessions.dc.html'],
    ['skills', 'Skills', ''],
    ['jobs', 'Jobs', ''],
    ['more', 'More', 'Commands.dc.html'],
  ];
  const cell = ([id, label, href], i) => {
    const on = id === active;
    const st = `display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; height: 100%; ${i < 4 ? 'border-right: 1px solid var(--rule); ' : ''}background: ${on ? 'var(--pbrand)' : 'transparent'}; color: ${on ? 'var(--pbrand-on)' : 'var(--ink-2)'}; position: relative`;
    const inner = `${icon(id, 10)}<span class="mw" style="font-size: 6.5px; line-height: 1">${label}</span>`;
    return href
      ? `<a href="${href}"${on ? ' aria-current="page"' : ''} style="${st}">${inner}</a>`
      : `<button type="button" style="${st}">${inner}</button>`;
  };
  return `<nav aria-label="Sections" style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); height: 25px; flex: none; background: var(--paper-2); border-bottom: 1px solid var(--ink)">
${T.map(cell).join('\n')}
</nav>`;
}

// Nous DS buttons at native px.
export const BEVEL = 'inset -1px -1px 0 0 rgba(0,0,0,.45), inset 1px 1px 0 0 rgba(255,255,255,.45)';
export const BEVEL_SOFT = 'inset -1px -1px 0 0 rgba(0,0,0,.14), inset 1px 1px 0 0 rgba(255,255,255,.95)';
export const btnPrimary = (h = 26) => `height: ${h}px; display: flex; align-items: center; gap: 7px; padding: 0 9px; background: var(--pbrand); color: var(--pbrand-on); box-shadow: ${BEVEL}; position: relative`;
export const btnSecondary = (h = 26) => `height: ${h}px; display: flex; align-items: center; gap: 6px; padding: 0 8px; background: var(--paper-2); color: var(--ink); border: 1px solid var(--rule); box-shadow: ${BEVEL_SOFT}; position: relative`;
export const bracket = (label, size = 7.5) => `<span class="cp" style="font-size: ${size}px; line-height: 1; white-space: nowrap"><span style="opacity: .5">[</span> ${label} <span style="opacity: .5">]</span></span>`;

// ---------------------------------------------------------------- device shell
// opts.hot: face buttons to light, e.g. ['A'] ; opts.notif: 'off' | 'blink' | 'on'
export function device(top, bottom, opts = {}) {
  const hot = new Set(opts.hot || []);
  const notif = opts.notif || 'off';
  const face = (k, x, y) => {
    const on = hot.has(k);
    return `<span style="position: absolute; left: ${x}px; top: ${y}px; width: 34px; height: 34px; border-radius: 17px; display: flex; align-items: center; justify-content: center; font-family: 'Rules Expanded', sans-serif; font-weight: 700; font-size: 12px; ${on
      ? 'background: radial-gradient(circle at 36% 30%, #ffffff 0%, var(--accent) 40%, #b8b800 100%); color: var(--accent-ink); box-shadow: 0 3px 0 rgba(0,0,0,.45), 0 0 0 3px rgba(255,255,255,.9)'
      : 'background: radial-gradient(circle at 36% 30%, var(--btn-hi) 0%, var(--btn) 72%); color: var(--btn-ink); box-shadow: 0 3px 0 rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.16)'}">${k}</span>`;
  };
  const notifStyle = notif === 'blink'
    ? 'background: var(--accent); box-shadow: 0 0 10px 4px rgba(242,242,0,.6); animation: hx-blink 1.2s step-end infinite'
    : notif === 'on'
      ? 'background: #8f8fff; box-shadow: 0 0 8px 3px rgba(143,143,255,.55)'
      : 'background: rgba(0,0,0,.35); box-shadow: inset 0 1px 1px rgba(0,0,0,.5)';
  const holes = 'background: radial-gradient(circle, rgba(0,0,0,.6) 1.5px, rgba(0,0,0,0) 2px) 0 0/7px 7px';
  const rim = (r) => `<div aria-hidden="true" style="position: absolute; left: 12px; top: 10px; right: 12px; bottom: 14px; border-radius: ${r}; background: linear-gradient(180deg, rgba(255,255,255,.07), rgba(0,0,0,.12)); box-shadow: inset 0 0 0 1px rgba(255,255,255,.08), inset 0 -1px 0 rgba(0,0,0,.3)"></div>
<div aria-hidden="true" style="position: absolute; left: 12px; top: 10px; right: 12px; bottom: 14px; border-radius: ${r}; background: linear-gradient(110deg, rgba(255,255,255,.17) 0%, rgba(255,255,255,0) 20%, rgba(255,255,255,0) 72%, rgba(255,255,255,.06) 86%, rgba(255,255,255,0) 100%)"></div>`;
  const shellBg = (deg) => `background: linear-gradient(${deg}deg, var(--shell-hi) 0%, var(--shell) 34%, var(--shell-lo) 100%)`;

  return `<div aria-hidden="true" style="position: absolute; left: 70px; right: 70px; bottom: -14px; height: 44px; border-radius: 50%; background: radial-gradient(closest-side, rgba(0,0,24,.42), rgba(0,0,24,0))"></div>

<div style="position: absolute; left: 0; top: 0; width: 960px; height: 560px; border-radius: 46px 46px 22px 22px; ${shellBg(172)}; box-shadow: inset 0 2px 0 rgba(255,255,255,.30), inset 0 -3px 0 rgba(0,0,0,.30), inset 0 0 0 1px rgba(0,0,0,.35), 0 18px 40px -16px rgba(0,0,30,.6)">
${rim('38px 38px 14px 14px')}
<div aria-hidden="true" style="position: absolute; left: 473px; top: 8px; width: 14px; height: 14px; border-radius: 7px; background: radial-gradient(circle at 42% 38%, #4a4a80 0 1.6px, #05050f 2.6px); box-shadow: 0 0 0 2px rgba(0,0,0,.35), 0 1px 0 2px rgba(255,255,255,.12)"></div>
<div aria-hidden="true" style="position: absolute; left: 916px; top: 20px; width: 9px; height: 9px; border-radius: 5px; ${notifStyle}"></div>
<div aria-hidden="true" style="position: absolute; left: 26px; top: 286px; width: 21px; height: 56px; ${holes}"></div>
<div aria-hidden="true" style="position: absolute; left: 913px; top: 286px; width: 21px; height: 56px; ${holes}"></div>
<div aria-hidden="true" style="position: absolute; left: 914px; top: 92px; width: 12px; height: 122px; border-radius: 6px; background: rgba(0,0,0,.42); box-shadow: inset 0 2px 3px rgba(0,0,0,.6), 0 1px 0 rgba(255,255,255,.12)"><div style="position: absolute; left: -3px; top: 84px; width: 18px; height: 24px; border-radius: 4px; background: linear-gradient(180deg, #f4f4ff, #9c9cba); box-shadow: 0 2px 2px rgba(0,0,0,.45)"></div></div>
<span aria-hidden="true" style="position: absolute; left: 910px; top: 224px; font-family: 'Rules Expanded', sans-serif; font-weight: 700; font-size: 10px; color: var(--shell-ink)">3D</span>
<span aria-hidden="true" style="position: absolute; left: 930px; top: 226px; width: 6px; height: 6px; border-radius: 3px; background: #1b1b2c; box-shadow: inset 0 1px 1px rgba(0,0,0,.6)"></span>
<div aria-hidden="true" style="position: absolute; left: 62px; top: 24px; width: 836px; height: 512px; border-radius: 12px; background: linear-gradient(180deg, #0b0b16, #030308); box-shadow: inset 0 1px 0 rgba(255,255,255,.09), 0 0 0 1px rgba(0,0,0,.5)"></div>
<span aria-hidden="true" style="position: absolute; left: 82px; top: 523px; font-family: 'Rules Expanded', sans-serif; font-weight: 700; font-size: 7px; letter-spacing: .32em; color: rgba(255,255,255,.34)">HERMES AGENT</span>
<span aria-hidden="true" style="position: absolute; right: 82px; top: 523px; font-family: 'Rules Expanded', sans-serif; font-weight: 400; font-size: 7px; letter-spacing: .32em; color: rgba(255,255,255,.22)">NOUS RESEARCH</span>
<div style="position: absolute; left: 80px; top: 40px; width: 800px; height: 480px; overflow: hidden; background: var(--scr); border-radius: 2px">
<div style="zoom: 2; width: 400px; height: 240px; position: relative; overflow: hidden; color: var(--tx); font-size: 9px">
${field()}
${top}
</div>
<div class="glass matte" aria-hidden="true"></div>
</div>
</div>

<div aria-hidden="true" style="position: absolute; left: 0; top: 576px; width: 960px; height: 604px; border-radius: 22px 22px 58px 58px; ${shellBg(176)}; box-shadow: inset 0 2px 0 rgba(255,255,255,.24), inset 0 -4px 0 rgba(0,0,0,.32), inset 0 0 0 1px rgba(0,0,0,.35), 0 28px 50px -18px rgba(0,0,30,.65)"></div>
<div style="position: absolute; left: 0; top: 576px; width: 960px; height: 604px; border-radius: 22px 22px 58px 58px">
${rim('16px 16px 50px 50px')}
<div aria-hidden="true" style="position: absolute; left: 32px; top: 58px; width: 104px; height: 104px; border-radius: 52px; background: radial-gradient(circle at 50% 42%, rgba(0,0,0,.5), rgba(0,0,0,.28) 72%); box-shadow: inset 0 3px 7px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.14)${hot.has('pad') ? ', 0 0 0 3px rgba(255,255,255,.9)' : ''}">
<div style="position: absolute; left: 17px; top: 15px; width: 70px; height: 70px; border-radius: 35px; background: repeating-radial-gradient(circle at 50% 50%, rgba(255,255,255,.05) 0 2px, rgba(255,255,255,0) 2px 5px), radial-gradient(circle at 38% 30%, var(--btn-hi) 0%, var(--btn) 72%); box-shadow: 0 4px 5px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.16)"></div>
</div>
<div aria-hidden="true" style="position: absolute; left: 55px; top: 222px; width: 58px; height: 58px">
<div style="position: absolute; left: 19px; top: 0; width: 20px; height: 58px; border-radius: 3px; background: linear-gradient(90deg, var(--btn-hi), var(--btn)); box-shadow: 0 3px 0 rgba(0,0,0,.45)"></div>
<div style="position: absolute; left: 0; top: 19px; width: 58px; height: 20px; border-radius: 3px; background: linear-gradient(180deg, var(--btn-hi), var(--btn)); box-shadow: 0 3px 0 rgba(0,0,0,.45)"></div>
<div style="position: absolute; left: 24px; top: 24px; width: 10px; height: 10px; border-radius: 5px; background: radial-gradient(circle, rgba(0,0,0,.45), rgba(0,0,0,0) 70%)"></div>
</div>
<div aria-hidden="true" style="position: absolute; left: 0; top: 0">
${face('X', 866, 72)}${face('Y', 826, 112)}${face('A', 906, 112)}${face('B', 866, 152)}
</div>
<div aria-hidden="true" style="position: absolute; left: 146px; top: 26px; width: 668px; height: 508px; border-radius: 10px; background: linear-gradient(180deg, #0b0b16, #030308); box-shadow: inset 0 1px 0 rgba(255,255,255,.09), 0 0 0 1px rgba(0,0,0,.5)"></div>
<div style="position: absolute; left: 160px; top: 40px; width: 640px; height: 480px; overflow: hidden; background: var(--paper); border-radius: 2px">
<div class="dots" style="zoom: 2; width: 320px; height: 240px; position: relative; overflow: hidden; color: var(--ink); font-size: 9px">
${bottom}
</div>
<div class="glass matte" aria-hidden="true"></div>
</div>
<div aria-hidden="true" style="position: absolute; left: 243px; top: 547px; width: 474px; height: 14px; display: flex; gap: 12px">
<span style="flex: 1; border-radius: 7px; background: linear-gradient(180deg, var(--btn-hi), var(--btn)); box-shadow: 0 2px 0 rgba(0,0,0,.42)"></span>
<span style="flex: 1; border-radius: 7px; background: linear-gradient(180deg, var(--btn-hi), var(--btn)); box-shadow: 0 2px 0 rgba(0,0,0,.42)"></span>
<span style="flex: 1; border-radius: 7px; background: linear-gradient(180deg, var(--btn-hi), var(--btn)); box-shadow: 0 2px 0 rgba(0,0,0,.42)"></span>
</div>
<div aria-hidden="true" style="position: absolute; left: 243px; top: 566px; width: 474px; display: flex; gap: 12px; font-family: 'Rules Expanded', sans-serif; font-weight: 700; font-size: 7px; letter-spacing: .26em; color: var(--shell-ink)">
<span style="flex: 1; text-align: center">SELECT</span>
<span style="flex: 1; display: flex; justify-content: center">${icon('home', 11)}</span>
<span style="flex: 1; text-align: center">START</span>
</div>
<div aria-hidden="true" style="position: absolute; left: 477px; top: 586px; width: 5px; height: 5px; border-radius: 3px; background: rgba(0,0,0,.6)"></div>
<div aria-hidden="true" style="position: absolute; left: 878px; top: 470px; width: 28px; height: 28px; border-radius: 14px; display: flex; align-items: center; justify-content: center; background: radial-gradient(circle at 38% 30%, var(--btn-hi), var(--btn) 75%); color: var(--btn-ink); box-shadow: 0 2px 0 rgba(0,0,0,.45)">${icon('power', 12)}</div>
<div aria-hidden="true" style="position: absolute; left: 862px; top: 512px; width: 6px; height: 6px; border-radius: 3px; background: #8f8fff; box-shadow: 0 0 6px 2px rgba(143,143,255,.6)"></div>
<div aria-hidden="true" style="position: absolute; left: 876px; top: 512px; width: 6px; height: 6px; border-radius: 3px; background: rgba(0,0,0,.35)"></div>
</div>

<div aria-hidden="true" style="position: absolute; left: 34px; top: 551px; width: 892px; height: 32px; border-radius: 16px; background: linear-gradient(180deg, var(--shell-deep) 0%, var(--shell-lo) 20%, var(--shell-hi) 48%, var(--shell-lo) 80%, var(--shell-deep) 100%); box-shadow: 0 3px 6px rgba(0,0,0,.35)">
<span style="position: absolute; left: 128px; top: 2px; width: 2px; height: 28px; background: rgba(0,0,0,.35)"></span>
<span style="position: absolute; right: 128px; top: 2px; width: 2px; height: 28px; background: rgba(0,0,0,.35)"></span>
<span style="position: absolute; right: 52px; top: 12px; width: 8px; height: 8px; border-radius: 4px; background: ${opts.wifi === false ? 'rgba(0,0,0,.35)' : '#8f8fff'}; box-shadow: ${opts.wifi === false ? 'none' : '0 0 8px 3px rgba(143,143,255,.55)'}"></span>
</div>`;
}

// ---------------------------------------------------------------- artboard file
export const ROOT_JS = `const rootClass = this.props.motion === false ? 'still' : '';`;

export function page({ title, body, width = 960, height = 1180, extraProps = {}, script = '' }) {
  const props = {
    motion: { editor: 'boolean', default: true, section: 'Look' },
    ...extraProps,
    $preview: { width, height },
  };
  const logic = script || `class Component extends DCLogic {
renderVals() {
${ROOT_JS}
return { rootClass };
}
}`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${title}</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="${GFONTS}" rel="stylesheet">
<style>
${CSS}
</style>
</helmet>
<div class="hx {{rootClass}}" style="width: ${width}px; height: ${height}px; position: relative; font-family: 'Space Grotesk', 'M PLUS 1p', sans-serif; color: var(--tx)">
${body}
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='${JSON.stringify(props).replace(/&/g, '&amp;').replace(/'/g, '&#39;')}'>
${logic}
</script>
</body>
</html>
`;
}
