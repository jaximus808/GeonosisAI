'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { ConnectionStatus } from './ConnectionStatus';
import { LlmBackendChip } from './llm/LlmBackendChip';
import { ThemeToggle } from './brand/ThemeToggle';
import { Wordmark } from './brand/Wordmark';

const NAV = [
  { href: '/dashboard', label: 'Tickets' }, // TDM-111: `/` is the public landing page
  { href: '/tickets/new', label: 'New ticket' },
  { href: '/agents', label: 'Agents' },
  { href: '/factory', label: 'Factory' }, // TDM-112
  { href: '/events', label: 'Event log' },
  { href: '/marketplace', label: 'Tasks' },
  { href: '/earnings', label: 'Earnings' }, // TDM-64
];

/** Contributor phone pages get the compact PhoneTask header (TDM-109). */
const PHONE_PREFIXES = ['/marketplace', '/join', '/earnings', '/teleop', '/reset', '/practice', '/sell'];
const PHONE_NAV = [
  { href: '/marketplace', label: 'Tasks' },
  { href: '/earnings', label: 'Earnings' },
];

function isActive(path: string, href: string) {
  if (href === '/dashboard') return path === '/dashboard' || (path.startsWith('/tickets/') && !path.startsWith('/tickets/new'));
  return path === href || path.startsWith(`${href}/`);
}

/** TDM-109: GeonosisAI header bar. Wordmark left, nav with a melt underline on the current page. */
export function Header() {
  const path = usePathname() ?? '/';
  const phone = PHONE_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));
  if (path === '/') {
    // TDM-111: the public landing page. No operator nav (those pages need the code or a login).
    return (
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[1120px] items-center gap-4 px-4 py-3 sm:px-8">
          <Link href="/" className="no-underline" aria-label="GeonosisAI home">
            <Wordmark size="sm" />
          </Link>
          <nav className="ml-auto flex items-center gap-4 text-sm text-ink-muted" aria-label="Landing">
            <Link href="/live" className="hidden whitespace-nowrap hover:text-ink sm:inline">
              Watch live
            </Link>
            <Link href="/ops/login?next=/dashboard" className="whitespace-nowrap hover:text-ink">
              Operator login
            </Link>
            <ThemeToggle />
          </nav>
        </div>
      </header>
    );
  }
  if (phone) {
    return (
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[1120px] items-center gap-4 px-4 py-3">
          <Link href="/marketplace" className="no-underline" aria-label="GeonosisAI tasks">
            <Wordmark size="sm" />
          </Link>
          <nav className="flex gap-4 text-sm text-ink-muted" aria-label="Contributor">
            {PHONE_NAV.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                aria-current={isActive(path, n.href) ? 'page' : undefined}
                className="hover:text-ink aria-[current]:text-ink"
              >
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <ConnectionStatus />
          </div>
        </div>
      </header>
    );
  }
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-[1120px] flex-wrap items-center gap-x-6 gap-y-1 px-4 sm:px-8">
        <Link href="/dashboard" className="py-3 no-underline" aria-label="GeonosisAI dashboard">
          <Wordmark size="sm" />
        </Link>
        <nav className="gx-nav" aria-label="Main">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} aria-current={isActive(path, n.href) ? 'page' : undefined}>
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3 py-2">
          <LlmBackendChip />
          <ConnectionStatus />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
