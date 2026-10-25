// TDM-111: the public landing page's live numbers, computed from the anonymous SpacetimeDB subscription
// (robot, task, ticket are public tables). Pure, so test/tdm111-landing.test.ts runs it under node.
// A number the page cannot back with rows is null, and the page hides it (never a placeholder value).

import { tdm107IsSimRobot } from '@/lib/tdm107/robotMode';
import { isSampleRobot, isSampleTicket } from '@/lib/sample/sample';

/** Robot statuses that count as online (same list as the new-ticket form, TDM74_ROBOT_ONLINE). */
export const TDM111_ROBOT_ONLINE = ['online', 'busy', 'paused'];

type RobotRow = { name: string; status: string; cameraConfigJson: string };
type TaskRow = { status: string; priceCents: bigint | number };
type TicketRow = { id: bigint | number; status: string; taskDescription?: string; robotId?: bigint | number };

export type Tdm111Stats = {
  robotsOnline: { total: number; sim: number; real: number } | null;
  openTasks: { count: number; payCents: number } | null;
  shipped: number | null;
  latest: { id: number; status: string } | null;
};

/** Tickets the landing page counts as filed: every ticket except the seeded [Sample] ones. */
export function tdm111FiledCount(tickets: readonly TicketRow[]): number {
  return tickets.filter((t) => !isSampleTicket(t)).length;
}

export function tdm111Stats(
  robots: readonly RobotRow[] | null,
  tasks: readonly TaskRow[] | null,
  tickets: readonly TicketRow[] | null,
): Tdm111Stats {
  let robotsOnline: Tdm111Stats['robotsOnline'] = null;
  if (robots) {
    // test.* robots are fixtures (ignored by robot auto-selection too)
    // sample-* robots belong to the seeded [Sample] tickets, never a live robot
    const on = robots.filter((r) => !r.name.startsWith('test.') && !isSampleRobot(r) && TDM111_ROBOT_ONLINE.includes(r.status));
    const sim = on.filter((r) => tdm107IsSimRobot(r)).length;
    robotsOnline = { total: on.length, sim, real: on.length - sim };
  }
  let openTasks: Tdm111Stats['openTasks'] = null;
  if (tasks) {
    const open = tasks.filter((t) => t.status === 'open');
    openTasks = { count: open.length, payCents: open.reduce((s, t) => s + Number(t.priceCents), 0) };
  }
  let shipped: number | null = null;
  let latest: Tdm111Stats['latest'] = null;
  if (tickets) {
    // [Sample] tickets are seeded examples (scripts/demo/seed_samples.py), not real runs: left out of every number
    tickets = tickets.filter((t) => !isSampleTicket(t));
    shipped = tickets.filter((t) => t.status === 'shipped').length;
    for (const t of tickets) {
      const id = Number(t.id);
      if (!latest || id > latest.id) latest = { id, status: t.status };
    }
  }
  return { robotsOnline, openTasks, shipped, latest };
}
