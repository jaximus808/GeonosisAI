import type { ReactNode } from 'react';

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="mb-8">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Placeholder({ owner, children }: { owner: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-sm text-zinc-500 dark:border-zinc-700">
      {children ?? 'Not built yet.'} <span className="text-zinc-400">Owner: {owner}.</span>
    </div>
  );
}
