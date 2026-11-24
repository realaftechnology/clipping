// Renders an overlay HTML timeline to transparent PNGs with headless Chrome (DevTools Protocol, no npm deps).
// Only frames whose content changed are captured; static holds become one PNG with a duration.
//   node render.mjs <overlay.html> <workDir> --frames N [--fps 60000/1001] [--step 2] [--workers 6]
//   node render.mjs <overlay.html> <workDir> --stills 3.5,16.5,81        (preview stills only)
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const html = path.resolve(args[0]), work = path.resolve(args[1]);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const fpsArg = opt('--fps', '60000/1001');
const FPS = fpsArg.includes('/') ? fpsArg.split('/').reduce((a, b) => a / b) : +fpsArg;
const NFRAMES = +opt('--frames', 0), STEP = +opt('--step', 2), WORKERS = +opt('--workers', 6);
const stills = opt('--stills', null);
const [W, H] = opt('--size', '1920x1080').split('x').map(Number);
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
fs.mkdirSync(work, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];

async function openPage(n) {
  const port = 9300 + Math.floor(Math.random() * 600) + n;
  const proc = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${path.join(work, 'chrome-' + n)}`,
    '--hide-scrollbars', '--force-device-scale-factor=1', `--window-size=${W},${H}`, '--no-first-run', '--force-color-profile=srgb',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding', 'about:blank'], { stdio: 'ignore' });
  procs.push(proc);
  let info;
  for (let i = 0; i < 80 && !info; i++) {
    try { info = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json(); } catch { await sleep(250); }
  }
  if (!info) throw new Error('Chrome did not start');
  const ws = new WebSocket(info.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  ws.onmessage = m => { const d = JSON.parse(m.data); const p = pending.get(d.id); if (p) { pending.delete(d.id); d.error ? p.rej(new Error(d.error.message)) : p.res(d.result); } };
  const send = (method, params = {}) => new Promise((res, rej) => { pending.set(++id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); });
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
  await send('Page.navigate', { url: 'file://' + html });
  const ev = async expr => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text); return r.result.value; };
  for (let i = 0; i < 100; i++) { if (await ev('window.ready').catch(() => null) === true) break; await sleep(100); if (i === 99) throw new Error('overlay page never became ready (did it call start()?)'); }
  const shot = async () => Buffer.from((await send('Page.captureScreenshot', { format: 'png', optimizeForSpeed: true })).data, 'base64');
  return { ev, shot };
}

let code = 0;
try {
  const t0 = Date.now();
  if (stills) {
    const p = await openPage(0);
    for (const t of stills.split(',').map(Number)) { await p.ev(`setTime(${t})`); fs.writeFileSync(path.join(work, `still_${t.toFixed(2)}.png`), await p.shot()); }
  } else {
    if (!NFRAMES) throw new Error('--frames required');
    const first = await openPage(0);
    const keys = await first.ev(`scan(${NFRAMES},${FPS},${STEP})`);
    const tScan = Date.now();
    let next = 0;
    const run = async p => { for (;;) { const i = next++; if (i >= keys.length) break; await p.ev(`setTime(${keys[i] / FPS})`); fs.writeFileSync(path.join(work, `k_${String(keys[i]).padStart(6, '0')}.png`), await p.shot()); } };
    const n = Math.max(1, Math.min(WORKERS, Math.ceil(keys.length / 40)));
    const pages = [first, ...await Promise.all(Array.from({ length: n - 1 }, (_, i) => openPage(i + 1)))];
    await Promise.all(pages.map(run));
    // Each key frame starts half a frame early so rounding can never delay it past its video frame.
    const start = f => Math.max(0, (f - 0.5) / FPS);
    let list = 'ffconcat version 1.0\n';
    keys.forEach((f, i) => { const end = i + 1 < keys.length ? start(keys[i + 1]) : NFRAMES / FPS; list += `file 'k_${String(f).padStart(6, '0')}.png'\nduration ${(end - start(f)).toFixed(6)}\n`; });
    list += `file 'k_${String(keys[keys.length - 1]).padStart(6, '0')}.png'\n`;
    fs.writeFileSync(path.join(work, 'overlay.ffconcat'), list);
    console.log(`rendered ${keys.length} unique frames of ${NFRAMES} (scan ${((tScan - t0) / 1000).toFixed(1)}s, capture ${((Date.now() - tScan) / 1000).toFixed(1)}s, ${n} workers)`);
  }
} catch (e) { console.error('render failed:', e.message); code = 1; }
for (const p of procs) p.kill();
process.exit(code);
