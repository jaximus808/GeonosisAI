// TDM-59 / TDM-106: practice mode for first-time operators.
//   /practice                            the sim's default scene + readiness check
//   /practice?ticket=12                  practise on the scene TDM-104 generated for ticket 12
//   /practice?robot=3                    practise on the scene deployed on sim robot 3
//   /practice?next=/marketplace?task=7   return there once ready ("Back to the task")
// Same controls as /teleop/[taskId], driving a client-side copy of the sim robot and its scene in the
// browser (lib/teleop/tdm106SceneSim.ts). No robot, no claim, no lease, no recording.

import { PracticeConsole } from '@/components/teleop/PracticeConsole';
import type { Tdm106SceneFetch } from '@/lib/teleop/tdm106PracticeScene';

export const metadata = { title: 'Practice driving · Robot Deployment Factory' };

const ORCH = (process.env.RDF_ORCHESTRATOR_URL || process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');

async function fetchScene(ticketId?: string, robotId?: string): Promise<Tdm106SceneFetch | null> {
  if (!ticketId && !robotId) return null;
  const path = ticketId ? `tickets/${ticketId}/scene` : `robots/${robotId}/scene`;
  try {
    const res = await fetch(`${ORCH}/${path}`, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    if (res.status === 404) return { ticketId, robotId, body: null };
    if (!res.ok) return { ticketId, robotId, body: null, error: `orchestrator answered ${res.status}` };
    return { ticketId, robotId, body: await res.json() };
  } catch (e) {
    return { ticketId, robotId, body: null, error: (e as Error).name === 'TimeoutError' ? 'orchestrator timed out' : 'orchestrator not reachable' };
  }
}

export default async function PracticePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const q = await searchParams;
  const one = (k: string) => (typeof q[k] === 'string' ? (q[k] as string) : undefined);
  const raw = one('next');
  // only same-site paths
  const next = raw && raw.startsWith('/') && !raw.startsWith('//') ? raw : undefined;
  const ticket = one('ticket');
  const robot = one('robot');
  const scene = await fetchScene(ticket && /^\d{1,15}$/.test(ticket) ? ticket : undefined, robot && /^\d{1,15}$/.test(robot) ? robot : undefined);
  return (
    <>
      <h1 className="mb-2 text-xl font-semibold tracking-tight">Practise driving the arm</h1>
      <PracticeConsole next={next} scene={scene} />
    </>
  );
}
