import { header, hints, icon, kb, tabs } from '../lib.mjs';

const model = (name, detail, opts = {}) => `<div role="radio" aria-checked="${opts.inUse ? 'true' : 'false'}" style="height: 30px; display: flex; align-items: center; gap: 8px; padding: 0 8px; ${opts.focus ? 'background: var(--blue); color: var(--on)' : 'color: var(--tx)'}; ${opts.first ? '' : 'border-top: 1px solid var(--line)'}">
<span class="mono" style="width: 14px; font-size: 8px; ${opts.focus ? 'color: var(--on)' : 'color: var(--blue)'}">${opts.focus ? '▸' : ''}</span>
<span class="mono" style="font-size: 8px; ${opts.focus ? 'color: var(--on)' : 'color: var(--blue)'}">${opts.inUse ? '(•)' : '( )'}</span>
<span style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px">
<span class="mono" style="font-size: 8.5px; font-weight: 700">${name}</span>
<span style="font-size: 6.5px; ${opts.focus ? 'color: var(--on-2)' : 'color: var(--tx-3)'}">${detail}</span>
</span>
${opts.inUse ? `<span class="eb" style="font-size: 5px; padding: 2.5px 4px; border: 1px solid var(--blue); color: var(--blue)">In use</span>` : ''}
${opts.focus ? `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 7px; font-weight: 700">Switch${kb('A')}</span>` : ''}
</div>`;

const top = `
${header({ title: '<span class="mono" style="color: var(--on)">/model</span>', link: 'LAN' })}

<div style="position: absolute; left: 12px; top: 24px; width: 376px; display: flex; align-items: baseline; gap: 8px">
<h2 class="disp" style="margin: 0; font-size: 24px; color: var(--tx)">Switch model</h2>
<span style="font-size: 8px; color: var(--tx-3)">for this session · takes effect on the next message</span>
</div>

<div style="position: absolute; left: 12px; top: 52px; width: 376px; background: var(--paper-2); border: 1px solid var(--tx); box-shadow: 3px 3px 0 var(--blue)">
${model('qwen3.8-27b', 'Local · llama.cpp on :8080 · 65k context', { first: true, inUse: true })}
${model('saluki-27b', 'Local · llama.cpp on :8080 · 32k context', { focus: true })}
${model('gemma-4-27b', 'Local · second server on :8081 · 65k context')}
${model('deepseek-chat', 'DeepSeek API · cloud · billed per token')}
</div>

<p class="mono" style="position: absolute; left: 12px; top: 186px; margin: 0; width: 376px; display: flex; justify-content: space-between; font-size: 6.5px; color: var(--tx-3)"><span>4 models on RANDOM-PC</span><span>avg 21 tok/s · last switch 2 days ago</span></p>

${hints([['A', 'Switch'], ['B', 'Back']], '↕ Choose')}
`;

const cmd = (name, desc, opts = {}) => {
  const tag = opts.href ? 'a' : 'button';
  const st = `display: flex; flex-direction: column; justify-content: center; gap: 3px; padding: 0 7px; text-align: left; ${opts.cls || ''}${opts.on ? 'background: var(--pbrand); color: var(--pbrand-on)' : `color: ${opts.bad ? 'var(--pbad)' : 'var(--ink)'}`}`;
  return `<${tag}${opts.href ? ` href="${opts.href}"` : ' type="button"'} style="${st}">
<span class="mono" style="font-size: 8px; font-weight: 700"><span style="opacity: .55">/</span>${name}${opts.bad ? ' !' : ''}</span>
<span style="font-size: 6.5px; ${opts.on ? 'color: rgba(255,255,255,.8)' : 'color: var(--ink-3)'}">${desc}</span>
</${tag}>`;
};

const seg = (label, on = false, first = false) => `<button type="button" style="display: flex; align-items: center; justify-content: center; ${first ? '' : 'border-left: 1px solid var(--ink); '}${on ? 'background: var(--ink); color: var(--paper)' : 'color: var(--ink-2)'}"><span class="eb" style="font-size: 5px">${label}</span></button>`;

const L = 'border-left: 1px solid var(--rule); ', T = 'border-top: 1px solid var(--rule); ';

const bottom = `
<div style="position: absolute; inset: 0; display: flex; flex-direction: column">
${tabs('more')}
<div style="flex: 1; padding: 7px 8px 8px; display: flex; flex-direction: column; gap: 6px">
<div style="display: flex; align-items: center; gap: 7px">
<h2 class="disp" style="margin: 0; font-size: 21px; color: var(--ink)">Commands</h2>
<div style="flex: 1; height: 22px; display: flex; align-items: center; gap: 5px; padding: 0 6px; background: var(--paper-2); border: 1px solid var(--ink)"><span class="mono" style="font-size: 9px; font-weight: 700; color: var(--pbrand)">/</span><span style="font-size: 8px; color: var(--ink-3)">Type or tap a command</span></div>
</div>
<div style="height: 17px; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--ink); background: var(--paper-2)">
${seg('Session', false, true)}${seg('Agent', true)}${seg('Setup')}${seg('Tools')}
</div>
<div style="flex: 1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: repeat(3, minmax(0, 1fr)); background: var(--paper-2); border: 1px solid var(--rule)">
${cmd('model', 'Switch model', { on: true })}
${cmd('reasoning', 'Thinking depth', { cls: L })}
${cmd('personality', 'Voice and tone', { cls: L })}
${cmd('fast', 'Quick replies', { cls: T })}
${cmd('voice', 'Spoken replies', { cls: T + L, href: 'Voice.dc.html' })}
${cmd('approvals', 'Ask before risky', { cls: T + L, href: 'Approval.dc.html' })}
${cmd('status', 'Session info', { cls: T })}
${cmd('usage', 'Tokens and cost', { cls: T + L })}
${cmd('yolo', 'Skip approvals', { cls: T + L, bad: true })}
</div>
</div>
</div>
`;

export default {
  file: 'Commands.dc.html',
  title: 'Hermes 3DS: Commands and model',
  board: '6 · Commands & model',
  device: { hot: [], notif: 'on' },
  top,
  bottom,
};
