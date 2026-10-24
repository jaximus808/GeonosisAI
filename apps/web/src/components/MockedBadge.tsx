/** Label for anything stubbed or mocked (CONTEXT.md: never pass a mock off as real). */
export function MockedBadge({ label = 'Mocked', title }: { label?: string; title?: string }) {
  return (
    <span
      title={title ?? 'This part is stubbed or mocked, not live data.'}
      className="inline-flex items-center rounded border border-amber-400/60 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
    >
      {label}
    </span>
  );
}
