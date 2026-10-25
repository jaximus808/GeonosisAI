import { Tdm116DashboardShell } from '@/components/cockpit/DashboardShell';

// TDM-111: the operator dashboard (it was `/` until the public landing page took that path).
// TDM-116: the same app shell as the ticket page: robots and their current job across the top, the tickets
// on the left, the selected ticket's cockpit on the right (`?ticket=N`). Contributors and the full event
// log have their own pages (/marketplace, /events).
export default async function Dashboard({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  const t = Array.isArray(sp.ticket) ? sp.ticket[0] : sp.ticket;
  return <Tdm116DashboardShell ticketParam={t ?? null} />;
}
