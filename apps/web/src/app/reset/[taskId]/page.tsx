// TDM-62: the reset task page for the person at the robot's table (phone-first).
//   /reset/<taskId>   steps + start photo, arm safety, claim, "Done, robot may continue",
//                     then payout (TEST MODE) and the robot resuming, live.
// Logic: components/tdm62/ResetTaskView.tsx, lib/tdm62/reset.ts; API: /api/tdm62 -> orchestrator /resets.

import { ResetTaskView } from '@/components/tdm62/ResetTaskView';

export default async function ResetPage({ params }: { params: Promise<{ taskId: string }> }) {
  const { taskId } = await params;
  const id = /^\d{1,19}$/.test(taskId) ? BigInt(taskId) : 0n;
  return <ResetTaskView taskId={id} />;
}
