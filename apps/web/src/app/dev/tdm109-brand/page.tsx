import { MockedBadge } from '@/components/MockedBadge';
import {
  Button,
  GX_STATION_OF_STATUS,
  Line,
  Meter,
  Plate,
  Readout,
  SimTag,
  StatusTag,
  Tag,
  TaskCard,
  Wordmark,
  gxTicketStations,
} from '@/components/brand';

export const metadata = { title: 'Brand components · GeonosisAI' };

const P = (n: number, k: number) => Array.from({ length: n }, (_, i) => ({ ok: i < k }));

/** TDM-109: the GeonosisAI components with example numbers, for review and screenshots. Not live data. */
export default function BrandGallery() {
  return (
    <div className="grid gap-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="gx-display-l">Brand components</h1>
        <MockedBadge label="Example numbers" title="Every number on this page is an example from the brand mocks, not live data." />
      </div>

      <Plate title="Wordmark">
        <div className="grid gap-4">
          <Wordmark size="hero" />
          <Wordmark descriptor />
          <Wordmark size="sm" />
        </div>
      </Plate>

      <Plate title="Status tags" aside={<span className="gx-id">ticket.status</span>}>
        <div className="flex flex-wrap gap-2">
          {Object.keys(GX_STATION_OF_STATUS)
            .filter((s) => s !== 'onboard')
            .map((s) => (
              <StatusTag key={s} status={s} />
            ))}
          <Tag tone="money">Purchased</Tag>
          <Tag>Sleeping</Tag>
          <SimTag />
        </div>
      </Plate>

      <Plate title="The line" aria-label="Line">
        <div className="grid gap-6">
          <Line
            stations={gxTicketStations('eval', {
              notes: { baseline: '3/10', diagnose: '2 clusters', plan_data: '8 takeovers', dispatch: '8/8', review: '7 accepted', train: '4,000 steps', eval: '9/10', shipped: 'target 80%' },
            })}
          />
          <Line stations={gxTicketStations('failed', { stoppedAt: 'train', notes: { baseline: '3/10', train: 'GPU quota' } })} />
        </div>
      </Plate>

      <div className="grid gap-6 lg:grid-cols-2">
        <Plate title="Before and after" aside={<span className="gx-id">Wilson 95%</span>}>
          <div className="grid gap-6 sm:grid-cols-2">
            <Readout label="Baseline · pi05-base" successes={3} trials={10} target={0.8} trialChips={P(10, 3)} />
            <Readout label="Iteration 2 · pi05-cup-bin-it2" successes={9} trials={10} target={0.8} pass trialChips={P(10, 9)} />
          </div>
        </Plate>
        <Plate title="Planner decision" aside={<Tag>Claude</Tag>}>
          <p className="mt-0">6 of 7 baseline failures happen in the last 5 cm. 1 of 7 knocks the cup over on approach.</p>
          <p className="font-semibold">Buy recovery takeovers, not clean demos.</p>
          <div className="gx-small flex justify-between">
            <span>Spent</span>
            <span className="gx-data text-ochre">$31.50 of $50.00</span>
          </div>
          <Meter className="mt-2" value={3150} max={5000} label="Spent 31.50 of 50 dollars" />
        </Plate>
      </div>

      <Plate title="Buttons">
        <div className="flex flex-wrap gap-3">
          <Button variant="primary">Ship checkpoint</Button>
          <Button>View report</Button>
          <Button variant="danger">Soft e-stop</Button>
          <Button variant="ghost">Cancel</Button>
          <Button disabled disabledReason="Waiting for 8 accepted episodes">
            Start training
          </Button>
        </div>
      </Plate>

      <div className="gx-phone" data-testid="gx-phone-mock">
        <div className="flex items-baseline justify-between">
          <span className="gx-label">Earned today</span>
          <strong className="font-mono text-[28px] leading-8 font-medium text-ochre">$12.50</strong>
        </div>
        <h2 className="font-display m-0 text-xl font-bold">Open tasks</h2>
        <TaskCard
          kind="Recovery takeover"
          priceCents={350}
          title="Catch the cup at the bin rim"
          instructions="The robot drives. When it pauses at the bin, you take over and finish the drop."
          facts={['About 90 s', 'yam-01', '3 of 8 left']}
          action={
            <Button variant="primary" block>
              Claim task
            </Button>
          }
        />
        <TaskCard kind="Reset" priceCents={100} title="Put the cup back on the start mark" facts={['About 20 s', 'yam-01', 'In person']} action={<Button block>Claim task</Button>} />
        <TaskCard kind="Clean demo" priceCents={250} title="Drop the cup in the bin" facts={['About 60 s', 'yam-01']} taken />
        <p className="gx-small m-0">Test mode. No real money moves.</p>
      </div>
    </div>
  );
}
