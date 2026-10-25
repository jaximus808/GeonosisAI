// Real tickets on a teleop-only arm become a TDM-125 split (form note), and /dashboard ticket links open
// /tickets/<id>. `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const rm = (await import(new URL('../src/lib/tdm107/robotMode.ts', import.meta.url).href)) as typeof import('../src/lib/tdm107/robotMode');
const ck = (await import(new URL('../src/lib/tdm116/cockpit.ts', import.meta.url).href)) as typeof import('../src/lib/tdm116/cockpit');
const src = (p: string) => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8');

const cfg = (o: Record<string, unknown>) => ({ cameraConfigJson: JSON.stringify(o) });
const teleopOnly = cfg({ sim: false, teleop: true, arm: {}, stream: {} });

describe('teleop-only real arm', () => {
  it('can eval: sim yes, policy:true yes, teleop without policy no, legacy real yes', () => {
    assert.equal(rm.tdm107CanEval(cfg({ sim: true })), true);
    assert.equal(rm.tdm107CanEval(teleopOnly), false);
    assert.equal(rm.tdm107CanEval(cfg({ sim: false, teleop: true, policy: false })), false);
    assert.equal(rm.tdm107CanEval(cfg({ sim: false, teleop: true, policy: true })), true);
    assert.equal(rm.tdm107CanEval(cfg({ cameras: [] })), true);
    assert.equal(rm.tdm107IsTeleopOnly(teleopOnly), true);
    assert.equal(rm.tdm107IsTeleopOnly(null), false);
  });
  it('the split note matches the server note', () => {
    assert.equal(
      rm.tdm125SplitNote('yam-01'),
      "yam-01 can collect data but can't run evals tonight, so this ticket evaluates on the sim robot and collects on yam-01.",
    );
  });
  it('the form shows the note for Real + teleop-only and still sends the request', () => {
    const f = src('components/tdm74/NewTicketForm.tsx');
    assert.match(f, /robotMode === 'real' && selectedRobot && tdm107IsTeleopOnly\(selectedRobot\)/);
    assert.match(f, /data-testid="tdm125-split-note"/);
    assert.match(f, /data-testid="tdm125-created-note"/);
    assert.match(f, /r\.data\.note/);
  });
});

describe('/dashboard ticket links', () => {
  it('hrefs', () => {
    assert.equal(ck.tdm116TicketHref(12n), '/tickets/12');
    assert.equal(ck.tdm116PreviewHref(12n), '/dashboard?ticket=12');
  });
  it('list items and the embedded cockpit title link to /tickets/<id>', () => {
    const d = src('components/cockpit/DashboardShell.tsx');
    assert.match(d, /<Link href=\{tdm116TicketHref\(t\.id\)\} className="tdm116-tkt"/);
    assert.match(d, /href=\{tdm116PreviewHref\(t\.id\)\}/);
    assert.match(d, /data-testid="dashboard-tickets"/);
    const top = src('components/cockpit/TopBar.tsx');
    assert.match(top, /data-testid="cockpit-title-link"/);
    assert.match(top, /href=\{tdm116TicketHref\(t\.id\)\}/);
    assert.match(src('components/cockpit/TicketCockpit.tsx'), /embedded=\{embedded\}/);
  });
});
