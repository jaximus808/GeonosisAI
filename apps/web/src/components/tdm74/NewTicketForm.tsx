'use client';

// TDM-74: the ticket-creation page body (/tickets/new). CONTEXT.md user flow step 2.
//
//   1. The person writes the job ("Put the cup in the bin, 80% reliable, $50 budget").
//   2. "Read the job" -> POST /tickets/parse: Claude's reading comes back as a work order, with each
//      value labelled by where it came from (the prompt, the person, or a default). Nothing is created.
//   3. Changing the robot, budget, target or Advanced settings refreshes the work order without
//      calling Claude again (the parsed reading is sent back). Editing the prompt marks it stale.
//   4. "Create ticket" -> POST /tickets with the same parsed reading, so the ticket is exactly what
//      was shown, then redirects to /tickets/[id]. Backend refusals render next to their field.
//
// Robots come live from SpacetimeDB; names starting test. are hidden unless the toggle is on.

import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState, type ReactNode, type Ref } from 'react';
import { useConnectionStatus, useRows } from '@/lib/spacetime/hooks';
import type { Tdm79Field, Tdm79Form } from '@/lib/tdm79/settings';
import {
  TDM74_DEFAULTS,
  TDM74_ROBOT_ONLINE,
  tdm74Body,
  tdm74Create,
  tdm74EmptyAdvanced,
  tdm74Preview,
  tdm74Usd,
  type Tdm74Errors,
  type Tdm74Field,
  type Tdm74Inputs,
  type Tdm74Preview,
} from '@/lib/tdm74/ticket';
import {
  TDM107_MODE_HINT,
  TDM107_MODE_LABEL,
  tdm107DefaultMode,
  tdm125DefaultCollectRobot,
  tdm107IsSimRobot,
  tdm107IsTeleopOnly,
  tdm125SplitLine,
  type Tdm107Mode,
} from '@/lib/tdm107/robotMode';

const TDM74_EXAMPLES = [
  'Put the cup in the bin, 80% reliable, $50 budget',
  'Stack the red block on the blue block, 9 out of 10 times, $120 budget',
  'Pick up the marker and drop it in the mug. Run 10 test trials. $75 to spend.',
  'Hand me the screwdriver from the tray',
];

type Source = 'prompt' | 'you' | 'default' | 'auto';
const SOURCE_LABEL: Record<Source, string> = {
  prompt: 'from your prompt',
  you: 'you set',
  default: 'default',
  auto: 'picked for you',
};

export function NewTicketForm() {
  const router = useRouter();
  const [prompt, setPrompt] = useState('');
  const [robotId, setRobotId] = useState('');
  const [robotTouched, setRobotTouched] = useState(false);
  // TDM-107: which robot the ticket runs on. Defaults to the real arm when one is online, else the sim.
  const [robotMode, setRobotMode] = useState<Tdm107Mode>('sim');
  const [modeTouched, setModeTouched] = useState(false);
  const [showTest, setShowTest] = useState(false);
  const [budgetUsd, setBudgetUsd] = useState('');
  const [targetPct, setTargetPct] = useState('');
  const [advanced, setAdvanced] = useState<Tdm79Form>(tdm74EmptyAdvanced);
  const [advOpen, setAdvOpen] = useState(false);

  const [preview, setPreview] = useState<Tdm74Preview>();
  const [previewPrompt, setPreviewPrompt] = useState('');
  const [reading, setReading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [serverErrors, setServerErrors] = useState<Tdm74Errors>({});
  const [showLocal, setShowLocal] = useState(false);

  const previewedKey = useRef('');
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const budgetRef = useRef<HTMLInputElement>(null);
  const robotsRef = useRef<HTMLFieldSetElement>(null);

  // ------------------------------------------------------------ robots (live)
  const conn = useConnectionStatus();
  const [robotRows, robotsReady] = useRows('robot');
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(t);
  }, []);
  const robots = useMemo(() => {
    const isTest = (r: (typeof robotRows)[number]) => r.name.startsWith('test.');
    const anyOnline = robotRows.filter((r) => TDM74_ROBOT_ONLINE.includes(r.status));
    const ofMode = (r: (typeof robotRows)[number]) => (robotMode === 'sim') === tdm107IsSimRobot(r);
    const online = anyOnline.filter(ofMode);
    return {
      shown: online.filter((r) => showTest || !isTest(r)).sort((a, b) => a.name.localeCompare(b.name)),
      hiddenTest: online.filter(isTest).length,
      offline: robotRows.filter(ofMode).length - online.length,
      realOnline: online.filter((r) => !isTest(r)),
      nonTestOnline: anyOnline.filter((r) => !isTest(r)),
    };
  }, [robotRows, showTest, robotMode]);

  useEffect(() => {
    if (modeTouched || !robotsReady) return;
    setRobotMode(tdm107DefaultMode(robots.nonTestOnline));
  }, [modeTouched, robotsReady, robots.nonTestOnline]);

  // Preselect the robot when exactly one non-test robot of the mode is online and the person hasn't chosen.
  useEffect(() => {
    if (robotTouched || !robotsReady) return;
    setRobotId(robots.realOnline.length === 1 ? String(robots.realOnline[0].id) : '');
  }, [robotTouched, robotsReady, robots.realOnline]);
  // A selection that is no longer listed (went offline, or test robots hidden) is dropped.
  useEffect(() => {
    if (robotId && robotsReady && !robots.shown.some((r) => String(r.id) === robotId)) setRobotId('');
  }, [robotId, robotsReady, robots.shown]);

  // ------------------------------------------------------------ request + validation
  const [collectRobotId, setCollectRobotId] = useState(''); // TDM-125: '' = collect on the same robot
  const [collectTouched, setCollectTouched] = useState(false);
  // TDM-125: a sim ticket collects on the online real arm by default (evals stay on the sim).
  useEffect(() => {
    if (collectTouched || !robotsReady) return;
    setCollectRobotId(robotMode === 'sim' ? tdm125DefaultCollectRobot(robots.nonTestOnline) : '');
  }, [collectTouched, robotsReady, robotMode, robots.nonTestOnline]);
  // A Real ticket on a teleop-only arm (no policy: true, e.g. yam-01 on tdm119_teleop tonight) can't run its
  // evals there. The request is sent anyway: the server makes it a split (evals on the sim robot, data on the arm).
  const selectedRobot = robots.shown.find((r) => String(r.id) === robotId);
  const simEvalRobot = robotRows.find((r) => tdm107IsSimRobot(r) && TDM74_ROBOT_ONLINE.includes(r.status) && !r.name.startsWith('test.'));
  const splitNote =
    robotMode === 'real' && selectedRobot && tdm107IsTeleopOnly(selectedRobot) ? tdm125SplitLine(simEvalRobot?.name ?? 'sim robot', selectedRobot.name) : null;
  const realRobots = useMemo(() => robotRows.filter((r) => !tdm107IsSimRobot(r) && (showTest || !r.name.startsWith('test.'))), [robotRows, showTest]);
  const inputs: Tdm74Inputs = { prompt, robotId, robotMode, budgetUsd, targetPct, advanced, collectRobotId };
  const stale = !!preview && prompt.trim() !== previewPrompt;
  const { body, errors: localErrors } = tdm74Body(inputs, preview && !stale ? preview.parsed : undefined);
  const localCount = Object.keys(localErrors).filter((k) => k !== 'prompt' || showLocal).length;
  const shown: Tdm74Errors = { ...serverErrors, ...(showLocal ? localErrors : omit(localErrors, 'prompt')) };
  const bodyKey = JSON.stringify(body);

  const clearErr = (...fields: Tdm74Field[]) =>
    setServerErrors((e) => {
      const next = { ...e };
      for (const f of [...fields, 'form' as const]) delete next[f];
      return next;
    });

  async function read() {
    setShowLocal(true);
    const fresh = tdm74Body(inputs); // no parsed: Claude reads the prompt again
    if (Object.keys(fresh.errors).length) {
      if (fresh.errors.prompt) promptRef.current?.focus();
      else if (Object.keys(fresh.errors).some((k) => !['prompt', 'robot', 'budget', 'target'].includes(k))) setAdvOpen(true);
      return;
    }
    setReading(true);
    setServerErrors({});
    const r = await tdm74Preview(fresh.body);
    setReading(false);
    if (r.ok) {
      previewedKey.current = JSON.stringify(tdm74Body(inputs, r.data.parsed).body); // already shown
      setPreview(r.data);
      setPreviewPrompt(String(fresh.body.prompt));
    } else {
      setServerErrors(r.errors);
    }
  }

  // Robot / budget / target / Advanced edits refresh the work order with the same parsed reading
  // (no Claude call). Debounced; skipped while the inputs don't validate.
  useEffect(() => {
    if (bodyKey === previewedKey.current) return;
    if (!preview || stale || reading || creating || Object.keys(localErrors).length) return;
    const t = window.setTimeout(async () => {
      setRefreshing(true);
      const r = await tdm74Preview(JSON.parse(bodyKey));
      setRefreshing(false);
      if (r.ok) {
        previewedKey.current = bodyKey;
        setPreview(r.data);
        setServerErrors({});
      } else setServerErrors(r.errors);
    }, 350);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bodyKey]);

  async function create() {
    if (!preview || stale) return;
    setShowLocal(true);
    if (Object.keys(localErrors).length) return;
    setCreating(true);
    setServerErrors({});
    const r = await tdm74Create(body);
    if (r.ok) {
      // The server may have changed the ticket (e.g. made it a split, r.data.note); the ticket page shows where it runs.
      router.push(`/tickets/${r.data.ticket_id}`);
      return; // keep the "Creating" state until the ticket page takes over
    }
    setCreating(false);
    setServerErrors(r.errors);
    if (r.errors.budget) budgetRef.current?.focus();
    else if (r.errors.robot) robotsRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    else if (Object.keys(r.errors).some((k) => !['prompt', 'robot', 'budget', 'target', 'form'].includes(k))) setAdvOpen(true);
  }

  const missingBudget = !!preview && !stale && preview.budget_cents === null;
  const robotProblem = !!preview && !stale && !!preview.robot_error;
  const blockers: string[] = [];
  if (!preview) blockers.push('Read the job first.');
  else if (stale) blockers.push('You changed the prompt. Read it again to update the work order.');
  else {
    if (missingBudget) blockers.push('Add a budget.');
    if (robotProblem) blockers.push('Choose a robot.');
    if (localCount) blockers.push('Fix the highlighted fields.');
  }

  const setAdv = (k: Tdm79Field, v: string | boolean) => {
    setAdvanced((f) => ({ ...f, [k]: v }));
    clearErr(k as Tdm74Field);
  };
  const advChanged = Object.keys((body.settings_overrides as object) ?? {}).length > 0;

  // ------------------------------------------------------------ render
  return (
    <div className="grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,1fr)_23rem]">
      <div className="min-w-0 space-y-9">
        <header>
          <h1 className="text-3xl font-semibold tracking-tight">Describe the job</h1>
          <p className="mt-2 max-w-prose text-zinc-600 dark:text-zinc-400">
            Say what the robot should do, how reliable it has to be and what you&apos;ll spend. Claude turns it into a work
            order you check before anything starts.
          </p>
        </header>

        {/* ---------------------------------------------------- the prompt */}
        <div>
          <label htmlFor="tdm74-prompt" className="sr-only">
            The job
          </label>
          <div
            className={`rounded-2xl border bg-white shadow-sm transition-colors focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-500/15 dark:bg-zinc-900 ${
              shown.prompt ? 'border-red-400 dark:border-red-700' : 'border-zinc-300 dark:border-zinc-700'
            }`}
          >
            <textarea
              id="tdm74-prompt"
              ref={promptRef}
              value={prompt}
              onChange={(e) => {
                setPrompt(e.target.value);
                clearErr('prompt');
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void read();
                }
              }}
              rows={4}
              maxLength={4000}
              placeholder="Put the cup in the bin, 80% reliable, $50 budget"
              aria-invalid={!!shown.prompt}
              aria-describedby="tdm74-prompt-err"
              className="block w-full resize-y rounded-t-2xl bg-transparent px-5 pt-4 pb-2 text-xl leading-relaxed text-zinc-900 outline-none placeholder:text-zinc-400 dark:text-zinc-50 dark:placeholder:text-zinc-600 sm:text-2xl sm:leading-relaxed"
            />
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3">
              <span className="text-xs text-zinc-500">
                <kbd className="rounded border border-zinc-300 px-1 font-sans dark:border-zinc-700">Ctrl</kbd> +{' '}
                <kbd className="rounded border border-zinc-300 px-1 font-sans dark:border-zinc-700">Enter</kbd> to read it
              </span>
              <button
                type="button"
                onClick={() => void read()}
                disabled={reading || creating}
                className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-wait disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                {reading && <Spinner />}
                {reading ? 'Reading…' : preview && !stale ? 'Read it again' : 'Read the job'}
              </button>
            </div>
          </div>
          <FieldError id="tdm74-prompt-err" msg={shown.prompt} />

          <div className="mt-4">
            <p className="text-sm text-zinc-500">Or start from one of these:</p>
            <ul className="mt-1.5 flex flex-col items-start gap-0.5">
              {TDM74_EXAMPLES.map((ex) => (
                <li key={ex}>
                  <button
                    type="button"
                    onClick={() => {
                      setPrompt(ex);
                      clearErr('prompt');
                      promptRef.current?.focus();
                    }}
                    className="rounded px-1 -mx-1 text-left text-sm text-blue-700 hover:bg-blue-50 hover:text-blue-900 focus-visible:outline-2 focus-visible:outline-blue-600 dark:text-blue-400 dark:hover:bg-blue-950/50 dark:hover:text-blue-300"
                  >
                    {ex}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* ---------------------------------------------------- robot */}
        <fieldset ref={robotsRef} className="min-w-0">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <legend className="text-base font-semibold">Robot</legend>
            <label className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
              <input
                type="checkbox"
                checked={showTest}
                onChange={(e) => setShowTest(e.target.checked)}
                className="h-4 w-4 rounded border-zinc-300 accent-blue-600"
              />
              Show test robots{robots.hiddenTest > 0 && !showTest ? ` (${robots.hiddenTest})` : ''}
            </label>
          </div>
          {/* TDM-107: Sim robot (MuJoCo 3D scene) or the real YAM. Switchable later by an operator. */}
          <div role="radiogroup" aria-label="Robot mode" className="mt-3 grid grid-cols-2 gap-2">
            {(['sim', 'real'] as const).map((m) => {
              const on = robotMode === m;
              return (
                <label
                  key={m}
                  className={`cursor-pointer rounded-lg border px-4 py-3 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-blue-600 ${
                    on
                      ? m === 'sim'
                        ? 'border-violet-500 bg-violet-50/70 dark:border-violet-500 dark:bg-violet-950/30'
                        : 'border-blue-500 bg-blue-50/60 dark:border-blue-500 dark:bg-blue-950/30'
                      : 'border-zinc-200 bg-white hover:border-zinc-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-700'
                  }`}
                >
                  <input
                    type="radio"
                    name="tdm107-robot-mode"
                    value={m}
                    checked={on}
                    onChange={() => {
                      setRobotMode(m);
                      setModeTouched(true);
                      setRobotTouched(false);
                      clearErr('robot');
                    }}
                    className="sr-only"
                  />
                  <span className="flex items-center gap-2 font-medium">
                    {TDM107_MODE_LABEL[m]}
                    {m === 'sim' && <SimChip />}
                  </span>
                  <span className="mt-0.5 block text-xs text-zinc-500">{TDM107_MODE_HINT[m]}</span>
                </label>
              );
            })}
          </div>
          <p className="mt-3 text-sm text-zinc-500">
            {robotMode === 'sim' ? 'Sim robots' : 'Real robots'} online now.
          </p>
          <div className="mt-3 space-y-2" role="radiogroup" aria-label="Robot" aria-describedby="tdm74-robot-err">
            {!robotsReady && (
              <p className="rounded-lg border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-500 dark:border-zinc-700">
                {conn.status === 'disconnected' ? `Can't reach SpacetimeDB: ${conn.error ?? 'disconnected'}` : 'Loading robots…'}
              </p>
            )}
            {robotsReady && robots.shown.length === 0 && (
              <p className="rounded-lg border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
                No {robotMode === 'sim' ? 'sim' : 'real'} robot is online{robots.hiddenTest > 0 && !showTest ? '. Show test robots to see more' : ''}.
              </p>
            )}
            {robots.shown.map((r) => {
              const id = String(r.id);
              const on = robotId === id;
              const beat = r.lastHeartbeat ? Math.max(0, Math.round((now - r.lastHeartbeat.toDate().getTime()) / 1000)) : null;
              return (
                <label
                  key={id}
                  className={`flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-blue-600 ${
                    on
                      ? 'border-blue-500 bg-blue-50/60 dark:border-blue-500 dark:bg-blue-950/30'
                      : 'border-zinc-200 bg-white hover:border-zinc-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-700'
                  }`}
                >
                  <input
                    type="radio"
                    name="tdm74-robot"
                    value={id}
                    checked={on}
                    onChange={() => {
                      setRobotId(id);
                      setRobotTouched(true);
                      clearErr('robot');
                    }}
                    className="h-4 w-4 accent-blue-600"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2 truncate font-medium">
                      {r.name}
                      {tdm107IsSimRobot(r) && <SimChip />}
                    </span>
                    <span className="block text-sm text-zinc-500">
                      #{id}, {r.embodiment.replace(/_/g, ' ')}
                      {beat !== null && `, heartbeat ${fmtAgo(beat)}`}
                    </span>
                  </span>
                  <StatusDot status={r.status} />
                </label>
              );
            })}
            {robotsReady && robots.offline > 0 && (
              <p className="text-xs text-zinc-500">
                {robots.offline} offline robot{robots.offline === 1 ? '' : 's'} not shown.
              </p>
            )}
          </div>
          <FieldError id="tdm74-robot-err" msg={shown.robot} />
          {splitNote && (
            <p className="mt-3 font-mono text-xs text-zinc-500" data-testid="tdm125-split-note">
              {splitNote}
            </p>
          )}
          {robotMode === 'sim' && realRobots.length > 0 && (
            <div className="mt-3" data-testid="tdm125-create-collect">
              <label className="block text-sm font-medium" htmlFor="tdm125-collect">
                Collect data on a real robot <span className="text-xs font-normal text-zinc-500">(optional)</span>
              </label>
              <select
                id="tdm125-collect"
                className="mt-1 w-full rounded-sm border border-zinc-300 bg-white px-2 py-1 text-sm"
                value={collectRobotId}
                onChange={(e) => { setCollectTouched(true); setCollectRobotId(e.target.value); }}
              >
                <option value="">No: collect on the sim robot too</option>
                {realRobots.map((r) => (
                  <option key={String(r.id)} value={String(r.id)}>
                    {r.name} (REAL) · {r.status}
                  </option>
                ))}
              </select>
            </div>
          )}
        </fieldset>

        {/* ---------------------------------------------------- budget + target */}
        <div className="grid gap-5 sm:grid-cols-2">
          <TextField
            id="tdm74-budget"
            inputRef={budgetRef}
            label="Budget"
            prefix="$"
            value={budgetUsd}
            onChange={(v) => {
              setBudgetUsd(v);
              clearErr('budget');
            }}
            placeholder={preview?.parsed.budget_usd != null ? String(preview.parsed.budget_usd) : '50'}
            hint="The most the factory may spend on data and training. Blank uses the prompt's."
            err={shown.budget}
            inputMode="decimal"
            highlight={missingBudget && !budgetUsd}
          />
          <TextField
            id="tdm74-target"
            label="Reliability target"
            suffix="%"
            value={targetPct}
            onChange={(v) => {
              setTargetPct(v);
              clearErr('target');
            }}
            placeholder={preview ? String(Math.round(preview.target_success * 1000) / 10) : '80'}
            hint="Share of eval trials that must succeed. Blank uses the prompt's, or 80%."
            err={shown.target}
            inputMode="decimal"
          />
        </div>

        {/* ---------------------------------------------------- advanced */}
        <details
          open={advOpen}
          onToggle={(e) => setAdvOpen((e.target as HTMLDetailsElement).open)}
          className="group rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
        >
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-5 py-4 focus-visible:outline-2 focus-visible:outline-blue-600 [&::-webkit-details-marker]:hidden">
            <span>
              <span className="font-semibold">Advanced run settings</span>
              <span className="block text-sm text-zinc-500">
                {advChanged ? 'Some settings differ from the defaults.' : 'Evaluation, data and training. The defaults suit most jobs.'}
              </span>
            </span>
            <svg aria-hidden viewBox="0 0 16 16" className="h-4 w-4 shrink-0 stroke-current text-zinc-500 transition-transform group-open:rotate-180" strokeWidth="1.5" fill="none">
              <path d="M4 6l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </summary>
          <div className="space-y-7 border-t border-zinc-200 px-5 py-5 dark:border-zinc-800">
            <AdvGroup title="Evaluation">
              <AdvField label="Trials per eval" field="eval_trials" form={advanced} set={setAdv} err={shown.eval_trials} hint="1 to 50" />
              <label className="flex items-start gap-3 text-sm sm:col-span-2">
                <input
                  type="checkbox"
                  checked={advanced.require_ci_low}
                  onChange={(e) => setAdv('require_ci_low', e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-zinc-300 accent-blue-600"
                />
                <span>
                  <span className="font-medium">Ship only when the confidence interval clears the target</span>
                  <span className="block text-zinc-500">Uses the lower bound of the 95% interval instead of the measured rate.</span>
                </span>
              </label>
            </AdvGroup>
            <AdvGroup title="Data">
              <AdvField label="Target episodes" field="target_episodes" form={advanced} set={setAdv} err={shown.target_episodes} hint="Demos to buy per round, 1 to 10,000" />
              <AdvField label="Max iterations" field="max_iterations" form={advanced} set={setAdv} err={shown.max_iterations} hint="Train-and-eval rounds, 1 to 20" />
              <div className="sm:col-span-2">
                <label htmlFor="tdm74-dto" className="block text-sm font-medium">
                  Kind of data
                </label>
                <select
                  id="tdm74-dto"
                  value={advanced.data_type_override}
                  onChange={(e) => setAdv('data_type_override', e.target.value)}
                  className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-950"
                >
                  <option value="">Let the planner decide</option>
                  <option value="clean_teleop">Clean teleop demos</option>
                  <option value="recovery_takeover">Recovery takeovers</option>
                </select>
                <FieldError msg={shown.data_type_override} />
              </div>
            </AdvGroup>
            <AdvGroup title="Training">
              <AdvField label="Learning rate" field="lr" form={advanced} set={setAdv} err={shown.lr} hint="e.g. 2.5e-5" mode="decimal" />
              <AdvField label="Steps" field="steps" form={advanced} set={setAdv} err={shown.steps} hint="1 to 1,000,000" />
              <AdvField label="Batch size" field="batch_size" form={advanced} set={setAdv} err={shown.batch_size} hint="1 to 4,096" />
              <AdvField label="LoRA rank" field="lora_rank" form={advanced} set={setAdv} err={shown.lora_rank} hint="0 to 1,024" />
              <div className="sm:col-span-2">
                <label htmlFor="tdm74-pp" className="block text-sm font-medium">
                  Policy instruction
                </label>
                <input
                  id="tdm74-pp"
                  value={advanced.prompt}
                  onChange={(e) => setAdv('prompt', e.target.value)}
                  placeholder={preview?.parsed.policy_prompt || 'Claude writes this from your prompt'}
                  className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-950"
                />
                <p className="mt-1 text-xs text-zinc-500">The sentence the robot policy is conditioned on. Blank uses Claude&apos;s.</p>
              </div>
            </AdvGroup>
            {advChanged && (
              <button
                type="button"
                onClick={() => setAdvanced(tdm74EmptyAdvanced())}
                className="text-sm font-medium text-zinc-600 underline underline-offset-2 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
              >
                Reset to defaults
              </button>
            )}
          </div>
        </details>
      </div>

      {/* ---------------------------------------------------- the work order */}
      <aside className="lg:sticky lg:top-6 lg:self-start" aria-live="polite">
        <WorkOrder
          preview={preview}
          stale={stale}
          reading={reading}
          refreshing={refreshing}
          inputs={inputs}
          robotChosen={robotTouched}
          onAddBudget={() => budgetRef.current?.focus()}
        />
        {shown.form && (
          <p role="alert" className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
            {shown.form}
          </p>
        )}
        <button
          type="button"
          onClick={() => void create()}
          disabled={creating || blockers.length > 0 || refreshing}
          className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-3 text-base font-semibold text-white shadow-sm hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-zinc-300 disabled:text-zinc-600 disabled:shadow-none dark:disabled:bg-zinc-800 dark:disabled:text-zinc-500"
        >
          {creating && <Spinner />}
          {creating ? 'Creating ticket…' : 'Create ticket'}
        </button>
        {!creating && blockers.length > 0 && <p className="mt-2 text-sm text-zinc-500">{blockers.join(' ')}</p>}
        {!creating && !blockers.length && (
          <p className="mt-2 text-sm text-zinc-500">The factory starts onboarding the robot as soon as the ticket exists.</p>
        )}
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------- work order

function WorkOrder({
  preview,
  stale,
  reading,
  refreshing,
  inputs,
  robotChosen,
  onAddBudget,
}: {
  robotChosen: boolean;
  preview?: Tdm74Preview;
  stale: boolean;
  reading: boolean;
  refreshing: boolean;
  inputs: Tdm74Inputs;
  onAddBudget: () => void;
}) {
  if (!preview) {
    return (
      <div className="rounded-2xl border border-dashed border-zinc-300 px-5 py-8 text-sm text-zinc-500 dark:border-zinc-700">
        {reading ? (
          <span className="inline-flex items-center gap-2">
            <Spinner /> Claude is reading the job…
          </span>
        ) : (
          <>
            <p className="font-medium text-zinc-700 dark:text-zinc-300">The work order appears here.</p>
            <p className="mt-1">Write the job and read it. You&apos;ll see how Claude understood it before any ticket is made.</p>
          </>
        )}
      </div>
    );
  }
  const p = preview;
  const ov = p.settings_overrides;
  const hp = (ov.hyperparams ?? {}) as Record<string, unknown>;
  const targetSrc: Source = inputs.targetPct.trim() ? 'you' : p.parsed.target_success != null ? 'prompt' : 'default';
  const budgetSrc: Source = inputs.budgetUsd.trim() ? 'you' : 'prompt';
  const trials = typeof ov.eval_trials === 'number' ? ov.eval_trials : TDM74_DEFAULTS.eval_trials;
  const trialsSrc: Source =
    inputs.advanced.eval_trials !== String(TDM74_DEFAULTS.eval_trials) ? 'you' : p.parsed.eval_trials != null ? 'prompt' : 'default';
  const robotSrc: Source = inputs.robotId && robotChosen ? 'you' : 'auto';
  const policy = String(hp.prompt ?? p.parsed.policy_prompt ?? '');
  const extra = Object.entries(ov).filter(([k]) => k !== 'eval_trials' && k !== 'hyperparams');
  const extraHp = Object.entries(hp).filter(([k]) => k !== 'prompt');

  return (
    <div
      className={`rounded-2xl border bg-white shadow-sm transition-opacity dark:bg-zinc-900 ${
        stale ? 'border-amber-400 opacity-70 dark:border-amber-600' : 'border-zinc-200 dark:border-zinc-800'
      } ${reading ? 'opacity-50' : ''}`}
    >
      <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-5 py-3 dark:border-zinc-800">
        <h2 className="text-sm font-semibold">Work order</h2>
        <span className="text-xs text-zinc-500">{refreshing ? 'Updating…' : stale ? 'Out of date' : 'Read by Claude'}</span>
      </div>
      {stale && (
        <p className="border-b border-amber-200 bg-amber-50 px-5 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          You changed the prompt since this was read.
        </p>
      )}
      <div className="px-5 pt-4 pb-5">
        <p className="text-lg leading-snug font-medium text-zinc-900 dark:text-zinc-50">{p.task_description}</p>
        {policy && (
          <p className="mt-1.5 text-sm text-zinc-500">
            Robot instruction: <q className="text-zinc-700 italic dark:text-zinc-300">{policy}</q>
          </p>
        )}
        <dl className="mt-4 divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
          <Row label="Reliability" value={`${Math.round(p.target_success * 1000) / 10}%`} source={targetSrc} />
          {p.budget_cents === null ? (
            <div className="flex items-center justify-between gap-3 py-2.5">
              <dt className="text-zinc-500">Budget</dt>
              <dd>
                <button
                  type="button"
                  onClick={onAddBudget}
                  className="rounded-md bg-amber-100 px-2 py-1 font-medium text-amber-900 hover:bg-amber-200 dark:bg-amber-900/50 dark:text-amber-100"
                >
                  None given. Add a budget
                </button>
              </dd>
            </div>
          ) : (
            <Row label="Budget" value={tdm74Usd(p.budget_cents)} source={budgetSrc} />
          )}
          <Row label="Eval trials" value={String(trials)} source={trialsSrc} />
          {p.robot_mode && <Row label="Runs on" value={TDM107_MODE_LABEL[p.robot_mode]} source={inputs.robotMode ? 'you' : 'auto'} />}
          {p.robot ? (
            <Row label="Robot" value={p.robot.name} source={robotSrc} />
          ) : (
            <div className="py-2.5">
              <div className="flex items-center justify-between gap-3">
                <dt className="text-zinc-500">Robot</dt>
                <dd className="font-medium text-red-700 dark:text-red-400">Not chosen</dd>
              </div>
              {p.robot_error && (
                <p className="mt-1 text-red-700 dark:text-red-400">{ROBOT_ERROR[p.robot_error.code] ?? p.robot_error.message}</p>
              )}
            </div>
          )}
          {extra.map(([k, v]) => (
            <Row key={k} label={SETTING_LABEL[k] ?? k} value={fmtSetting(k, v)} source="you" />
          ))}
          {extraHp.map(([k, v]) => (
            <Row key={k} label={SETTING_LABEL[k] ?? k} value={String(v)} source="you" />
          ))}
        </dl>
        <p className="mt-3 text-xs text-zinc-500">Settings not listed here keep their defaults.</p>
      </div>
    </div>
  );
}

const ROBOT_ERROR: Record<string, string> = {
  multiple_online_robots: 'Several robots are online. Pick one in the robot list.',
  no_online_robot: 'No robot is online. Start one, or show the test robots.',
  unknown_robot: 'That robot no longer exists. Pick another.',
  robot_mode_mismatch: 'That robot is of the other mode (sim vs real). Pick one from the list.',
};

/** TDM-107: sim is always visibly labelled. */
function SimChip() {
  return (
    <span className="rounded bg-violet-600 px-1.5 py-0.5 text-[10px] font-semibold tracking-wider text-white uppercase">
      Simulated
    </span>
  );
}

const SETTING_LABEL: Record<string, string> = {
  require_ci_low: 'Ship when',
  target_episodes: 'Target episodes',
  max_iterations: 'Max iterations',
  data_type_override: 'Kind of data',
  success_threshold: 'Ship threshold',
  lr: 'Learning rate',
  steps: 'Training steps',
  batch_size: 'Batch size',
  lora_rank: 'LoRA rank',
};

function fmtSetting(k: string, v: unknown): string {
  if (k === 'require_ci_low') return v ? 'Interval clears target' : 'Measured rate clears target';
  if (k === 'data_type_override') return v === 'clean_teleop' ? 'Clean teleop' : v === 'recovery_takeover' ? 'Recovery takeovers' : 'Planner decides';
  return String(v);
}

function Row({ label, value, source }: { label: string; value: string; source: Source }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-2.5">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="flex min-w-0 items-baseline gap-2 text-right">
        <span className="truncate font-medium text-zinc-900 dark:text-zinc-100">{value}</span>
        <span
          className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${
            source === 'you'
              ? 'bg-blue-50 text-blue-800 dark:bg-blue-950/60 dark:text-blue-300'
              : source === 'default'
                ? 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400'
                : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
          }`}
        >
          {SOURCE_LABEL[source]}
        </span>
      </dd>
    </div>
  );
}

// ---------------------------------------------------------------- small pieces

function omit<T extends object>(o: T, k: keyof T): T {
  const { [k]: _drop, ...rest } = o;
  return rest as T;
}

function fmtAgo(s: number): string {
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function StatusDot({ status }: { status: string }) {
  const tone = status === 'online' ? 'bg-emerald-500' : status === 'busy' ? 'bg-blue-500' : 'bg-amber-500';
  return (
    <span className="flex shrink-0 items-center gap-1.5 text-sm text-zinc-600 dark:text-zinc-400">
      <span aria-hidden className={`h-2 w-2 rounded-full ${tone}`} />
      {status}
    </span>
  );
}

function Spinner() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="h-4 w-4 animate-spin motion-reduce:animate-none" fill="none">
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function FieldError({ msg, id }: { msg?: string; id?: string }) {
  return (
    <p id={id} role={msg ? 'alert' : undefined} className={msg ? 'mt-1.5 text-sm text-red-700 dark:text-red-400' : 'sr-only'}>
      {msg ?? ''}
    </p>
  );
}

function TextField({
  id,
  label,
  value,
  onChange,
  placeholder,
  hint,
  err,
  prefix,
  suffix,
  inputMode,
  inputRef,
  highlight,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  hint?: string;
  err?: string;
  prefix?: string;
  suffix?: string;
  inputMode?: 'decimal' | 'numeric';
  inputRef?: Ref<HTMLInputElement>;
  highlight?: boolean;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-base font-semibold">
        {label}
      </label>
      <div
        className={`mt-1.5 flex items-center rounded-lg border bg-white px-3 focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-500/15 dark:bg-zinc-900 ${
          err ? 'border-red-400 dark:border-red-700' : highlight ? 'border-amber-400 ring-4 ring-amber-400/20' : 'border-zinc-300 dark:border-zinc-700'
        }`}
      >
        {prefix && <span className="text-zinc-500">{prefix}</span>}
        <input
          id={id}
          ref={inputRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          inputMode={inputMode}
          aria-invalid={!!err}
          aria-describedby={`${id}-hint ${id}-err`}
          className="w-full min-w-0 bg-transparent px-1.5 py-2.5 text-lg tabular-nums outline-none placeholder:text-zinc-400 dark:placeholder:text-zinc-600"
        />
        {suffix && <span className="text-zinc-500">{suffix}</span>}
      </div>
      {hint && (
        <p id={`${id}-hint`} className="mt-1 text-xs text-zinc-500">
          {hint}
        </p>
      )}
      <FieldError id={`${id}-err`} msg={err} />
    </div>
  );
}

function AdvGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset>
      <legend className="text-sm font-semibold text-zinc-700 dark:text-zinc-300">{title}</legend>
      <div className="mt-3 grid gap-4 sm:grid-cols-2">{children}</div>
    </fieldset>
  );
}

function AdvField({
  label,
  field,
  form,
  set,
  err,
  hint,
  mode = 'numeric',
}: {
  label: string;
  field: Tdm79Field;
  form: Tdm79Form;
  set: (k: Tdm79Field, v: string) => void;
  err?: string;
  hint?: string;
  mode?: 'numeric' | 'decimal';
}) {
  const id = `tdm74-adv-${field}`;
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        value={String(form[field])}
        onChange={(e) => set(field, e.target.value)}
        inputMode={mode}
        aria-invalid={!!err}
        className={`mt-1 w-full rounded-md border bg-white px-3 py-2 text-sm tabular-nums dark:bg-zinc-950 ${
          err ? 'border-red-400 dark:border-red-700' : 'border-zinc-300 dark:border-zinc-700'
        }`}
      />
      {hint && <p className="mt-1 text-xs text-zinc-500">{hint}</p>}
      <FieldError msg={err} />
    </div>
  );
}
