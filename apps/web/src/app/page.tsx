import type { Metadata } from 'next';
import Link from 'next/link';
import { ButtonLink } from '@/components/brand/Button';
import { GX_LINE_STATIONS, Line, gxTicketStations } from '@/components/brand/Line';
import { Wordmark } from '@/components/brand/Wordmark';
import { HeroSchematic } from '@/components/landing/HeroSchematic';
import { LandingLiveStrip } from '@/components/tdm111/LandingLiveStrip';

// TDM-111: the public landing page. Open without the access code (lib/tdm110/access.ts, public_guard.py);
// the operator dashboard moved to /dashboard. Text, the brand Line and four live numbers; the hero's 3D arm
// (components/landing/HeroSchematic) is the YAM as a hidden-line technical drawing, three.js loaded after first
// paint; the Anatomy foundry cell (HeroFoundry, pure SVG) is its no-WebGL fallback.

export const metadata: Metadata = {
  title: 'GeonosisAI: ticket in, deployed robot out',
  description:
    'File a ticket for a robot task. The foundry runs a baseline, buys the data that fixes the failures from people on their phones, fine-tunes π0.5 and ships it.',
};

const STATION_COPY: Record<string, string> = {
  baseline: 'The robot runs the task 10 times on its current policy, so the ticket starts from a measured number.',
  diagnose: 'Claude watches the failed trials and groups them by why they failed.',
  plan_data: 'The planner decides what data fixes those failures, how much, and what it costs inside the budget.',
  dispatch: 'Contributors claim paid tasks on their phones: teleop demos, takeover recoveries and resets.',
  review: 'Every episode is checked for gaps, jerk and success before anyone is paid.',
  train: 'π0.5 is fine-tuned with LoRA on one H100 on Modal. About 50 minutes per run.',
  eval: 'The new checkpoint runs 10 trials. The ticket shows the rate with its 95% interval.',
  shipped: 'If the rate clears the target, the checkpoint goes to the robot. If not, the line loops back.',
};

const BUILT_WITH: Array<[string, string]> = [
  ['SpacetimeDB', 'state, event log, live UI'],
  ['Factory Droids', 'long-running agents'],
  ['Modal', 'training and inference'],
  ['Claude', 'diagnosis and planning'],
  ['π0.5', 'the policy'],
  ['I2RT YAM', 'the arm'],
];

export default function Landing() {
  return (
    <div className="tdm111-landing flex flex-col gap-12 pb-8 sm:gap-16">
      {/* the hero wordmark scales with the viewport so it fits a 390px phone */}
      <style>{`.tdm111-landing .gx-wordmark-hero{font-size:clamp(46px,13vw,104px)}@media (min-width:640px){.tdm111-landing .tdm111-tag{font-size:56px;line-height:56px}}`}</style>

      <section
        className="grid grid-cols-1 gap-8 pt-4 sm:pt-10 min-[900px]:grid-cols-[minmax(480px,1.08fr)_minmax(0,1fr)] min-[900px]:items-center min-[900px]:gap-6"
        aria-labelledby="tdm111-tagline"
      >
        <div className="flex flex-col gap-6">
          <Wordmark size="hero" />
          <h1 id="tdm111-tagline" className="gx-display-l tdm111-tag max-w-[18ch]">
            Ticket in. Deployed robot out.
          </h1>
          <p className="max-w-[60ch] text-lg leading-7 text-ink-muted">
            File a ticket for a robot task. The foundry runs a baseline, buys exactly the data that fixes the failures
            from people on their phones, fine-tunes π0.5 and ships it.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <ButtonLink href="/join" variant="primary" className="justify-center">
              Earn by teaching a robot
            </ButtonLink>
            <ButtonLink href="/ops/login?next=/tickets/new" className="justify-center">
              File a ticket
            </ButtonLink>
            <Link href="/live" className="gx-small px-1 py-2 text-arc underline-offset-2 hover:underline sm:ml-2">
              Watch live
            </Link>
          </div>
        </div>
        {/* the showpiece: the YAM as a turning technical drawing (three.js, lazy; the box is reserved) */}
        <HeroSchematic className="h-[260px] min-[640px]:h-[340px] min-[900px]:-my-6 min-[900px]:h-[clamp(400px,38vw,500px)]" />
      </section>

      <LandingLiveStrip />

      <section aria-labelledby="tdm111-how" className="flex flex-col gap-6">
        <h2 id="tdm111-how" className="gx-display-l">
          How the line works
        </h2>
        {/* the rivet track on tablet and up; phones read the numbered list below */}
        <Line stations={gxTicketStations('shipped')} compactOnPhone={false} className="hidden sm:block" />
        <ol className="grid gap-px overflow-hidden rounded-sm border border-line bg-line sm:grid-cols-2 lg:grid-cols-4">
          {GX_LINE_STATIONS.map((s, i) => (
            <li key={s.key} className="flex flex-col gap-1 bg-surface p-4">
              <span className="gx-id">{String(i + 1).padStart(2, '0')}</span>
              <span className="gx-station-label">{s.name}</span>
              <p className="text-sm leading-6 text-ink-muted">{STATION_COPY[s.key]}</p>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="tdm111-two-ways" className="grid gap-4 md:grid-cols-2">
        <h2 id="tdm111-two-ways" className="sr-only">
          Two ways in
        </h2>
        <div className="gx-plate flex flex-col gap-3">
          <h3 className="gx-heading">Contributors</h3>
          <p className="text-sm leading-6 text-ink-muted">
            Open the link on your phone, claim a paid task, drive the arm from the browser or reset the scene. You
            need the 6-character code shown next to the QR.
          </p>
          <div>
            <ButtonLink href="/join" small>
              Enter the code
            </ButtonLink>
          </div>
        </div>
        <div className="gx-plate flex flex-col gap-3">
          <h3 className="gx-heading">Operators</h3>
          <p className="text-sm leading-6 text-ink-muted">
            Plug in a robot, file a ticket with a target success rate and a budget, then watch it move down the line.
            Operator login uses the operator passcode, not the contributor code.
          </p>
          <div className="flex flex-wrap gap-3">
            <ButtonLink href="/ops/login?next=/dashboard" small>
              Log in
            </ButtonLink>
          </div>
        </div>
      </section>

      <section aria-labelledby="tdm111-built" className="flex flex-col gap-3 border-t border-line pt-6">
        <h2 id="tdm111-built" className="gx-label">
          Built with
        </h2>
        <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          {BUILT_WITH.map(([name, role]) => (
            <li key={name}>
              <span className="font-medium text-ink">{name}</span> <span className="text-ink-muted">· {role}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
