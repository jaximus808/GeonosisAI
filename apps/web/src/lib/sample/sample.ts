// Sample tickets: fabricated example runs seeded by scripts/demo/seed_samples.py so the dashboard shows what
// real usage looks like. They are never real results, so every place that shows a ticket labels them SAMPLE,
// and the landing page's aggregate numbers leave them out. A ticket is a sample iff its task_description
// starts with "[Sample]" (the seeder writes that prefix on every one).

export const SAMPLE_PREFIX = '[Sample]';
/** The seeder's robot row (camera_config_json.sim = true, never online). */
export const SAMPLE_ROBOT_PREFIX = 'sample-';

export function isSampleTicket(t: { taskDescription?: string | null } | string | null | undefined): boolean {
  const d = typeof t === 'string' ? t : t?.taskDescription;
  return typeof d === 'string' && d.trimStart().startsWith(SAMPLE_PREFIX);
}

export function isSampleRobot(r: { name?: string | null } | null | undefined): boolean {
  return typeof r?.name === 'string' && r.name.startsWith(SAMPLE_ROBOT_PREFIX);
}
