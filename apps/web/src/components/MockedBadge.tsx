/** Label for anything stubbed or mocked (CONTEXT.md: never pass a mock off as real). */
export function MockedBadge({ label = 'Mocked', title }: { label?: string; title?: string }) {
  return (
    <span
      title={title ?? 'This part is stubbed or mocked, not live data.'}
      className="gx-tag gx-tag-cancelled"
    >
      <i className="gx-rivet" aria-hidden />
      {label}
    </span>
  );
}
