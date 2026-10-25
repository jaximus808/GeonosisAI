// TDM-126: the deployment page's "Real arm" runs. Pure helpers, no React and no runtime imports (tested in
// apps/web/test/tdm126-real-run.test.ts). The orchestrator decides for real (POST /tickets/{id}/deployment/run
// {real_arm, confirm, checkpoint}); these only decide what the page offers, with the same rules.

export const TDM126_CONFIRM = 'HANDS_ON_ESTOP';
export const TDM126_PURPOSE = 'deployment_real_run';
export type Tdm126Choice = 'before' | 'after';

type RobotLite = { id: bigint; name: string; status: string; cameraConfigJson: string };

function cfg(r: RobotLite | undefined | null): Record<string, unknown> {
  try {
    const c = JSON.parse(r?.cameraConfigJson || '{}') as unknown;
    return c && typeof c === 'object' ? (c as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** A real arm that advertises `policy: true` (the Jetson's tdm119_teleop with TDM119_ALLOW_POLICY=1). */
export function tdm126IsRealPolicyArm(r: RobotLite | undefined | null): boolean {
  if (!r) return false;
  const c = cfg(r);
  return c.sim !== true && c.policy === true;
}

/** A checkpoint the real YAM may run: a real Modal volume checkpoint (no fake://, no stub, no base pi0.5). */
export function tdm126RealCheckpointOk(uri: string | null | undefined): boolean {
  return !!uri && uri.startsWith('modal-vol://rdf-checkpoints/') && !uri.includes('fake') && !uri.includes('stub-no-gpu');
}

/** The real arm the page offers, same order as the orchestrator: the ticket's robot, its collect robot, else the
 * only online real policy arm (names starting `test.` ignored). undefined = no button. */
export function tdm126RealArm(
  ticketRobotId: bigint,
  collectRobotId: bigint,
  robots: readonly RobotLite[],
): RobotLite | undefined {
  const byId = (id: bigint) => robots.find((r) => r.id === id);
  for (const id of [ticketRobotId, collectRobotId]) {
    const r = byId(id);
    if (r && cfg(r).sim !== true) return tdm126IsRealPolicyArm(r) ? r : undefined;
  }
  const live = robots.filter((r) => tdm126IsRealPolicyArm(r) && ['online', 'idle'].includes(r.status) && !r.name.startsWith('test.'));
  return live.length === 1 ? live[0] : undefined;
}

/** The request body the page sends (the browser never names a checkpoint URI). */
export function tdm126Body(choice: Tdm126Choice, robotId: bigint): { real_arm: true; confirm: string; checkpoint: Tdm126Choice; robot_id: number } {
  return { real_arm: true, confirm: TDM126_CONFIRM, checkpoint: choice, robot_id: Number(robotId) };
}
