import Link from 'next/link';

// Branded 404 (Next's default is a white page under the dark header).
export default function NotFound() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-start gap-3 py-16">
      <span className="gx-tag">404</span>
      <h1 className="text-xl font-semibold">This page could not be found.</h1>
      <p className="text-ink-muted">The link may be old, or the task is already done.</p>
      <Link href="/marketplace" className="gx-btn gx-btn-primary">
        Open tasks
      </Link>
    </div>
  );
}
