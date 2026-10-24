// TDM-101: open /live at 1920x1080 in headless Chromium, take screenshots, and record every console
// error / page error for a soak period.
//   PLAYWRIGHT_DIR=<node_modules dir holding playwright> node spikes/tdm-101/shoot_live.mjs <url> <out-dir> [soak-seconds] [shot-every-s]
// Exit code 1 if any console error or page error was seen.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const req = createRequire(path.join(process.env.PLAYWRIGHT_DIR || process.cwd(), 'noop.js'));
const { chromium } = req('playwright');

const [url = 'http://127.0.0.1:3801/live', out = 'spikes/tdm-101/tmp', soakS = '20', everyS = '0'] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.TDM101_CHANNEL || 'chromium', args: ['--enable-unsafe-swiftshader', '--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist'] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push({ t: new Date().toISOString(), kind: 'console', text: m.text().slice(0, 4000) }); });
page.on('response', (r) => { if (r.status() >= 400) errors.push({ t: new Date().toISOString(), kind: 'http', text: `${r.status()} ${r.request().method()} ${r.url().slice(0, 200)}` }); });
page.on('pageerror', (e) => errors.push({ t: new Date().toISOString(), kind: 'pageerror', text: String(e.stack || e).slice(0, 4000) }));
// Operator login (the kiosk laptop is logged in as the operator, which is what lets the twin read the
// robot's state feed). The passcode is read from the repo .env and never printed.
const envFile = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../.env');
const pass = (fs.existsSync(envFile) ? fs.readFileSync(envFile, 'utf8') : '').split('\n')
  .map((l) => l.trim().replace(/^export\s+/, '')).find((l) => l.startsWith('RDF_OPERATOR_PASSCODE='))?.split('=').slice(1).join('=').replace(/^['"]|['"]$/g, '');
if (pass && process.env.TDM101_NO_LOGIN !== '1') {
  const r = await page.request.post(new URL('/api/ops/session', url).href, { data: { passcode: pass } });
  console.log(`operator login: ${r.status()}`);
}
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await page.screenshot({ path: path.join(out, 'live-1920x1080.png') });
const end = Date.now() + Number(soakS) * 1000;
let n = 0;
while (Date.now() < end) {
  await page.waitForTimeout(Math.min(5000, Math.max(0, end - Date.now())));
  if (Number(everyS) > 0 && (Date.now() - (end - Number(soakS) * 1000)) / 1000 >= (n + 1) * Number(everyS)) {
    n += 1;
    await page.screenshot({ path: path.join(out, `live-t${String(n).padStart(2, '0')}.png`) });
    const stage = await page.locator('[aria-current="step"]').first().textContent().catch(() => '');
    console.log(`[${new Date().toISOString()}] shot ${n}, stage: ${stage}, errors so far ${errors.length}`);
  }
}
await page.screenshot({ path: path.join(out, 'live-final.png') });
fs.writeFileSync(path.join(out, 'console-errors.json'), JSON.stringify(errors, null, 2));
console.log(`errors: ${errors.length}`);
for (const e of errors.slice(0, 20)) console.log(` ${e.kind}: ${e.text}`);
await browser.close();
process.exit(errors.length ? 1 : 0);
