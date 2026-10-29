// Builds every Hermes 3DS artboard.
//   node design/mockups/build.mjs            all boards
//   node design/mockups/build.mjs chat code  just these
// Writes design/canvas/project/<Name>.dc.html (published to the canvas) and
// design/canvas/preview/<Name>.html (plain pages for checking on http://localhost:8765/canvas/preview/).
// Preview pages read ?motion=0 to freeze animation.
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { page, device, LOCAL } from './lib.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const canvas = join(here, '..', 'canvas');
const outProject = join(canvas, 'project');
const outPreview = join(canvas, 'preview');
mkdirSync(outPreview, { recursive: true });
mkdirSync(outProject, { recursive: true });

// Board order on the canvas. A screen module that does not exist yet is skipped.
const NAMES = ['cover', 'connect', 'chat', 'approval', 'voice', 'sessions', 'commands', 'code', 'media'];
const screens = [];
const only = process.argv.slice(2);
for (const n of NAMES) {
  if (only.length && !only.includes(n)) continue;
  const f = join(here, 'screens', n + '.mjs');
  if (existsSync(f)) screens.push((await import('./screens/' + n + '.mjs')).default);
}

function build(s) {
  const body = s.body ?? device(s.top, s.bottom, s.device || {});
  return page({ title: s.title, body, width: s.width, height: s.height, extraProps: s.props, script: s.script });
}

// Turn a .dc.html into a plain page: helmet into head, blobs to local files,
// holes from s.preview, sc-if evaluated.
function preview(html, s) {
  const vals = { ...(s.preview || {}) };
  let h = html.replace('<script src="./support.js"></script>\n', '');
  const helmet = h.match(/<helmet>([\s\S]*?)<\/helmet>/)[1];
  h = h.replace(/<helmet>[\s\S]*?<\/helmet>\n?/, '').replace('</head>', `${helmet}</head>`);
  h = h.replace(/<x-dc>\n?|<\/x-dc>\n?/g, '');
  h = h.replace(/<script type="text\/x-dc"[\s\S]*?<\/script>\n?/, '');
  for (const [blob, local] of Object.entries(LOCAL)) h = h.split(blob).join(local);
  // sc-if, innermost first
  const ifRe = /<sc-if value="\{\{\s*([\w.]+)\s*\}\}"[^>]*>((?:(?!<sc-if)[\s\S])*?)<\/sc-if>/;
  for (let m; (m = h.match(ifRe));) h = h.replace(m[0], vals[m[1]] ? m[2] : '');
  h = h.replace(/\{\{\s*rootClass\s*\}\}/g, '');
  h = h.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (all, k) => (k in vals ? String(vals[k]) : ''));
  h = h.replace(/\son[A-Z]\w*="[^"]*"/g, '');
  const qs = `<script>if(new URLSearchParams(location.search).get('motion')==='0')document.querySelector('.hx').classList.add('still');</script>`;
  return h.replace('</body>', `${qs}\n</body>`);
}

for (const s of screens) {
  const html = build(s);
  writeFileSync(join(outProject, s.file), html);
  writeFileSync(join(outPreview, s.file.replace('.dc.html', '.html')), preview(html, s));
  console.log(`${s.file.padEnd(20)} ${(html.length / 1024).toFixed(1)} KB`);
}
