'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { ConnectionStatus } from './ConnectionStatus';
import { LlmBackendChip } from './llm/LlmBackendChip';
import { ThemeToggle } from './brand/ThemeToggle';
import { Wordmark } from './brand/Wordmark';

const NAV = [
  { href: '/dashboard', label: 'Tickets' }, // TDM-111: `/` is the public landing page
  { href: '/tickets/new', label: 'New ticket' },
  { href: '/agents', label: 'Agents' },
  { href: '/factory', label: 'Factory · 3D' }, // TDM-112 / TDM-115: the isometric factory cell
  { href: '/events', label: 'Event log' },
  { href: '/marketplace', label: 'Tasks' },
  { href: '/earnings', label: 'Earnings' }, // TDM-64
];

/** Contributor phone pages get the compact PhoneTask header (TDM-109). */
const PHONE_PREFIXES = ['/marketplace', '/join', '/earnings', '/teleop', '/reset', '/practice', '/sell', '/j'];
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
  // below md the operator nav folds into a menu button (seven links wrapped into three rows on a phone)
  const [menu, setMenu] = useState(false);
  useEffect(() => setMenu(false), [path]);
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
          <Link href="/marketplace" className="shrink-0 whitespace-nowrap no-underline" aria-label="GeonosisAI tasks">
            <Wordmark size="sm" />
          </Link>
          <nav className="flex min-w-0 gap-4 whitespace-nowrap text-sm text-ink-muted" aria-label="Contributor">
            {PHONE_NAV.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                aria-current={isActive(path, n.href) ? 'page' : undefined}
                className="-my-2 py-2 hover:text-ink aria-[current]:text-ink"
              >
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-3">
            <ConnectionStatus />
          </div>
        </div>
      </header>
    );
  }
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-[1120px] items-center gap-x-6 gap-y-1 px-4 sm:px-8 md:flex-wrap">
        <Link href="/dashboard" className="shrink-0 whitespace-nowrap py-3 no-underline" aria-label="GeonosisAI dashboard">
          <Wordmark size="sm" />
        </Link>
        <nav className="gx-nav max-md:hidden" aria-label="Main">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} aria-current={isActive(path, n.href) ? 'page' : undefined}>
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-3 py-2">
          <LlmBackendChip />
          <ConnectionStatus />
          <ThemeToggle />
          <button
            type="button"
            onClick={() => setMenu((m) => !m)}
            aria-expanded={menu}
            aria-controls="gx-mobile-nav"
            aria-label={menu ? 'Close menu' : 'Open menu'}
            className="-mr-2 flex h-11 w-11 items-center justify-center rounded-sm text-ink hover:bg-surface-sunken md:hidden"
            data-testid="gx-menu"
          >
            <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square">
              {menu ? <path d="M5 5l12 12M17 5L5 17" /> : <path d="M3 6h16M3 11h16M3 16h16" />}
            </svg>
          </button>
        </div>
      </div>
      {menu && (
        <nav id="gx-mobile-nav" className="grid grid-cols-2 gap-px border-t border-line bg-line md:hidden" aria-label="Main">
          {NAV.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              aria-current={isActive(path, n.href) ? 'page' : undefined}
              className="flex min-h-12 items-center bg-surface px-4 text-[15px] odd:last:col-span-2 text-ink-muted no-underline aria-[current]:text-ink aria-[current]:shadow-[inset_3px_0_0_var(--melt)]"
            >
              {n.label}
            </Link>
          ))}
        </nav>
      )}
    </header>
  );
}
