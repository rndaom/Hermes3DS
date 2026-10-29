import { header, hints, icon, kb, btnPrimary, btnSecondary, ART } from '../lib.mjs';

const top = `
${header({
  lead: `<span style="display: flex; color: var(--on)">${icon('image', 9)}</span><span class="eb" style="font-size: 5.5px; letter-spacing: .24em; color: var(--on)">Image 1 of 4</span>`,
  title: 'Clipper: export crash on long clips',
  link: 'LAN',
})}

<div style="position: absolute; left: 12px; top: 22px; width: 197px; height: 197px; border: 1px solid var(--tx); box-shadow: 3px 3px 0 var(--blue); overflow: hidden; background: #f4f0dc">
<img src="${ART}" alt="A giant statue reaching over a knight on worn stone steps, printed in blue on cream" style="width: 100%; height: 100%; object-fit: cover; display: block">
</div>

<div style="position: absolute; left: 224px; top: 24px; width: 164px; display: flex; flex-direction: column; gap: 6px">
<span class="eb" style="font-size: 5px; letter-spacing: .24em; color: var(--tx-3)">Made by image_gen · 9:44 PM</span>
<span class="mono" style="font-size: 9px; font-weight: 700; color: var(--tx)">olympus-dusk.png</span>
<span class="mono" style="font-size: 6.5px; color: var(--tx-3)">1024 × 1024 · PNG · 1.2 MB</span>
<p style="margin: 2px 0 0; padding-left: 6px; border-left: 2px solid var(--blue); font-size: 7.5px; line-height: 10.5px; color: var(--tx-2)">“A giant statue reaching over a small knight on old stone steps, blue engraving on cream.”</p>

<div style="margin-top: 6px; display: flex; align-items: flex-end; gap: 8px">
<div aria-hidden="true" style="position: relative; width: 46px; height: 46px; border: 1px solid var(--line-b); overflow: hidden">
<img src="${ART}" alt="" style="width: 100%; height: 100%; object-fit: cover; display: block; opacity: .55">
<span style="position: absolute; left: 0; top: 0; right: 0; bottom: 0; border: 1.5px solid var(--blue)"></span>
</div>
<div class="mono" style="display: flex; flex-direction: column; gap: 4px; font-size: 6.5px; color: var(--tx-3)">
<span><span style="padding: 1px 4px; background: var(--blue); color: var(--on); font-weight: 700">FIT</span> 20%</span>
<span>zoom <span style="color: var(--blue)">[■□□□□]</span></span>
<span>whole image shown</span>
</div>
</div>
</div>

${hints([['A', 'Zoom in'], ['B', 'Back']], 'Circle Pad pan · L R prev/next')}
`;

const tile = (txt) => `<span class="mono" style="width: 26px; height: 26px; flex: none; display: flex; align-items: center; justify-content: center; border: 1px solid var(--ink); background: var(--paper); font-size: 6.5px; font-weight: 700; color: var(--pbrand)">${txt}</span>`;

const item = (thumb, name, sub, sel = false, first = false) => `<button type="button" style="height: 33px; display: flex; align-items: center; gap: 7px; padding: 0 6px; text-align: left; color: var(--ink); ${first ? '' : 'border-top: 1px solid var(--rule); '}${sel ? 'background: var(--pbrand-soft); outline: 1.5px solid var(--pbrand); outline-offset: -1.5px; position: relative' : ''}">
${thumb}
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px"><span class="mono" style="font-size: 7.5px; ${sel ? 'font-weight: 700' : ''}">${name}</span><span style="font-size: 6.5px; color: var(--ink-3)">${sub}</span></span>
${sel ? `<span class="eb" style="font-size: 5px; color: var(--pbrand)">Viewing</span>` : `<span class="mono" style="font-size: 8px; color: var(--ink-3)">›</span>`}
</button>`;

const bottom = `
<div style="position: absolute; inset: 0; padding: 9px 10px 8px; display: flex; flex-direction: column; gap: 6px">
<div style="display: flex; align-items: center; justify-content: space-between">
<a href="Chat.dc.html" class="eb" style="display: inline-flex; align-items: center; gap: 3px; font-size: 5.5px; letter-spacing: .24em; color: var(--ink-3)">${icon('back', 8)}#08 · Chat</a>
<span class="mono" style="font-size: 7px; color: var(--ink-3)">4 files</span>
</div>
<h2 class="disp" style="margin: -1px 0 0; font-size: 25px; color: var(--ink)">Files in this chat</h2>
<div style="display: flex; flex-direction: column; background: var(--paper-2); border: 1px solid var(--rule)">
${item(`<img src="${ART}" alt="" style="width: 26px; height: 26px; flex: none; object-fit: cover; border: 1px solid var(--ink)">`, 'olympus-dusk.png', 'Image · 1.2 MB', true, true)}
${item(tile('PDF'), 'render-report.pdf', '12 pages · opens page by page')}
${item(tile('MP4'), 'frag-clip-03.mp4', '0:42 · small preview, full video on PC')}
${item(tile('MD'), 'patch-notes.md', 'Text · opens in reader view')}
</div>
<div style="margin-top: auto; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px">
<button type="button" style="${btnPrimary(24)}; justify-content: center; padding: 0 4px">${icon('save', 10)}<span style="font-size: 7.5px; font-weight: 700">Save to SD</span></button>
<button type="button" style="${btnSecondary(24)}; justify-content: center; padding: 0 4px">${icon('pc', 10)}<span style="font-size: 7.5px; font-weight: 500">Open on PC</span></button>
<a href="Chat.dc.html" style="${btnSecondary(24)}; justify-content: center; padding: 0 4px">${icon('chat', 10)}<span style="font-size: 7.5px; font-weight: 500">Ask about it</span></a>
</div>
</div>
`;

export default {
  file: 'Media.dc.html',
  title: 'Hermes 3DS: Images and attachments',
  board: '8 · Images & attachments',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
};
