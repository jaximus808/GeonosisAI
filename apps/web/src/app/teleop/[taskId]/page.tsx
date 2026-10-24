// TDM-56: teleop page for a claimed arm-control task (clean_teleop / recovery_takeover).
//   /teleop/<taskId>                                    claimant path (stream + control token from the orchestrator)
//   /teleop/<taskId>?dev_url=ws://host:port/ws/cameras&token=<t>[&control_url=ws://.../ws/control]
//                                                       fixed link, e.g. the spikes/tdm-56 mock robot (labelled in the UI)
//
// <TeleopSession> (components/teleop/TeleopSession.tsx) wires the parallel tickets into <Tdm56TeleopConsole>:
//   TDM-57 (exclusive control): useTeleopLease -> canDrive / canDriveReason / stamp, ControlLeaseBanner in controlSlot
//   TDM-58 (recording, takeover/handback): RecordingControls in recordingSlot, on useTdm56Teleop().controlSocket
//   TDM-59 (onboarding): Tdm59TeleopOnboarding before the console; its walkthrough blocks driving while open

import { ContributorPresence } from '@/components/ContributorPresence';
import { TeleopSession } from '@/components/teleop/TeleopSession';

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
      <TeleopSession taskId={taskId} dev={dev} />
    </>
  );
}
