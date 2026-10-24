// TDM-56: teleop page for a claimed arm-control task (clean_teleop / recovery_takeover).
//   /teleop/<taskId>                                    claimant path (stream + control token from the orchestrator)
//   /teleop/<taskId>?dev_url=ws://host:port/ws/cameras&token=<t>[&control_url=ws://.../ws/control]
//                                                       fixed link, e.g. the spikes/tdm-56 mock robot (labelled in the UI)
//
// Mount points for parallel tickets are props on <Tdm56TeleopConsole>:
//   TDM-57 (exclusive control): canDrive / canDriveReason / controlSlot
//   TDM-58 (recording, takeover/handback): recordingSlot, sending via useTdm56Teleop().send(...)

import { ContributorPresence } from '@/components/ContributorPresence';
import { Tdm56TeleopConsole } from '@/components/teleop/Tdm56TeleopConsole';

export default async function TeleopPage({
  params,
  searchParams,
}: {
  params: Promise<{ taskId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { taskId } = await params;
  const q = await searchParams;
  const one = (k: string) => (typeof q[k] === 'string' ? (q[k] as string) : undefined);
  const devUrl = one('dev_url');
  const dev = devUrl ? { streamUrl: devUrl, token: one('token') ?? '', controlUrl: one('control_url') } : undefined;

  return (
    <>
      <ContributorPresence page="teleop" />
      <h1 className="mb-3 text-xl font-semibold tracking-tight">Drive the arm for task {taskId}</h1>
      <Tdm56TeleopConsole taskId={taskId} dev={dev} />
    </>
  );
}
