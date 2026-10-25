// The SAMPLE label: an example ticket seeded for the demo (scripts/demo/seed_samples.py), not a real run.
import { isSampleTicket } from '@/lib/sample/sample';

const SAMPLE_TITLE =
  'Sample ticket: example records seeded to show what a finished run looks like. Not a real robot run; its numbers are not results.';

export function SampleBadge({ title }: { title?: string }) {
  return (
    <span
      title={title ?? SAMPLE_TITLE}
      className="gx-tag gx-tag-sim"
      data-testid="sample-badge"
      style={{ background: 'var(--slag-soft)', color: 'var(--slag)', boxShadow: 'inset 0 0 0 1px var(--slag)' }}
    >
      <i className="gx-rivet" aria-hidden />
      SAMPLE
    </span>
  );
}

/** Renders the badge only for a sample ticket (task_description starts with "[Sample]"). */
export function SampleBadgeFor({ description }: { description?: string | null }) {
  return isSampleTicket(description ?? '') ? <SampleBadge /> : null;
}
