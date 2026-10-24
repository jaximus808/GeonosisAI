import Link from 'next/link';
import { ConnectionStatus } from './ConnectionStatus';

const NAV = [
  { href: '/', label: 'Tickets' },
  { href: '/tickets/new', label: 'New ticket' },
  { href: '/agents', label: 'Agents' },
  { href: '/events', label: 'Event log' },
  { href: '/marketplace', label: 'Tasks' },
];

export function Header() {
  return (
    <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="font-semibold tracking-tight">
          Robot Deployment Factory
        </Link>
        <nav className="flex flex-wrap gap-4 text-sm text-zinc-600 dark:text-zinc-400">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} className="hover:text-zinc-900 dark:hover:text-zinc-100">
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto">
          <ConnectionStatus />
        </div>
      </div>
    </header>
  );
}
