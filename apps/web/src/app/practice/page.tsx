// TDM-59: practice mode for first-time operators.
//   /practice                         the simulated arm + readiness check
//   /practice?next=/marketplace?task=7  return there once ready ("Back to the task")
// Same controls as /teleop/[taskId], driving a simulated arm in the browser. No robot, no claim,
// no lease, no recording.

import { PracticeConsole } from '@/components/teleop/PracticeConsole';

export const metadata = { title: 'Practice driving · Robot Deployment Factory' };

export default async function PracticePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const q = await searchParams;
  const raw = typeof q.next === 'string' ? q.next : undefined;
  // only same-site paths
  const next = raw && raw.startsWith('/') && !raw.startsWith('//') ? raw : undefined;
  return (
    <>
      <h1 className="mb-1 text-xl font-semibold tracking-tight">Practise driving the arm</h1>
      <p className="mb-3 max-w-2xl text-sm text-zinc-600 dark:text-zinc-400">
        Same controls as a paid teleop task, on a simulated arm. Pick up the blue block and drop it in the bin. Take as long as you need.
      </p>
      <PracticeConsole next={next} />
    </>
  );
}
