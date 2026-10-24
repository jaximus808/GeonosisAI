import { JoinForm } from './JoinForm';

// TDM-60: contributor sign-up. A judge scans the dashboard QR (/join?ticket=N)
// and is a contributor in seconds: no passwords, no app download.
export default async function JoinPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const raw = (await searchParams).ticket;
  const ticket = typeof raw === 'string' && /^\d{1,18}$/.test(raw) && raw !== '0' ? raw : undefined;
  return (
    <div className="mx-auto max-w-md">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Join the factory</h1>
      <p className="mb-6 text-sm text-zinc-500">
        Get paid for robot resets, takeovers and teleop demos{ticket ? ` on ticket #${ticket}` : ''}. Works in your phone&apos;s browser.
      </p>
      <JoinForm ticket={ticket} />
    </div>
  );
}
