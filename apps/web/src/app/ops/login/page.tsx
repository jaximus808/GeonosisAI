import { OpsLoginForm } from './OpsLoginForm';

// Operator login: one shared passcode (RDF_OPERATOR_PASSCODE) -> a 12 h HttpOnly session cookie.
// Needed for robot motion commands and the other operator-only actions. The soft e-stop never is.
export default async function OpsLoginPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const raw = (await searchParams).next;
  // Same-origin paths only (no "//host" or "/\host" open redirects).
  const next = typeof raw === 'string' && /^\/(?![/\\])/.test(raw) ? raw : '/dashboard';
  return (
    <div className="mx-auto max-w-sm">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Operator login</h1>
      <p className="mb-6 text-sm text-zinc-500">
        Needed to move the robot, create tickets and change run settings. The soft e-stop works without it.
      </p>
      <OpsLoginForm next={next} />
    </div>
  );
}
