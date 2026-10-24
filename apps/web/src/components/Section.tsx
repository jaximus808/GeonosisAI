import type { ReactNode } from 'react';

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="mb-8">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="gx-station-label m-0 text-ink-muted">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Placeholder({ owner, children }: { owner: string; children?: ReactNode }) {
  return (
    <div className="rounded-sm border border-dashed border-line-strong p-6 text-sm text-ink-muted">
      {children ?? 'Not built yet.'} <span className="gx-id">Owner: {owner}.</span>
    </div>
  );
}
