'use client';

// TDM-79: run-settings editor, a drawer on the ticket page.
//
//   <RunSettingsButton ticketId={id} loop={...} liveUpdatedAt={settings?.updatedAt} />
//
// Reads GET /tickets/{id}/settings and writes PATCH /tickets/{id}/settings (TDM-20) through the
// /api/tdm79 proxy, sending only the fields that changed. Validation mirrors the orchestrator; any
// 422 it still returns is shown under the field it names. Each group says which step of which
// iteration will read the new value (see pickupNotes). The change is logged as human:<name>.

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  TDM79_TERMINAL,
  buildPatch,
  fetchSettings,
  patchSettings,
  pickupNotes,
  toForm,
  type Tdm79Errors,
  type Tdm79Field,
  type Tdm79Form,
  type Tdm79LoopState,
  type Tdm79Settings,
} from '@/lib/tdm79/settings';
import { Tdm107RobotModePanel } from '@/components/tdm107/RobotModePanel';

const NAME_KEY = 'rdf.tdm79.humanId';
const HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;

function readName(): string {
  try {
    return window.localStorage.getItem(NAME_KEY) ?? '';
  } catch {
    return '';
  }
}

function saveName(v: string) {
  try {
    window.localStorage.setItem(NAME_KEY, v);
  } catch {
    /* private window: the name just isn't remembered */
  }
}

export function RunSettingsButton({
  ticketId,
  loop,
  liveUpdatedAtMicros,
}: {
  ticketId: string;
  loop: Tdm79LoopState;
  /** run_settings.updated_at from the live subscription, to notice edits made elsewhere. */
  liveUpdatedAtMicros?: bigint;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-2 rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-800 shadow-sm hover:border-zinc-400 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:hover:bg-zinc-800"
      >
        <svg aria-hidden viewBox="0 0 16 16" className="h-4 w-4 fill-none stroke-current" strokeWidth="1.5">
          <path d="M2 4h7M12 4h2M2 12h2M7 12h7M9 2.5v3M5 10.5v3" strokeLinecap="round" />
        </svg>
        Run settings
      </button>
      {open && (
        <RunSettingsDrawer ticketId={ticketId} loop={loop} liveUpdatedAtMicros={liveUpdatedAtMicros} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

function RunSettingsDrawer({
  ticketId,
  loop,
  liveUpdatedAtMicros,
  onClose,
}: {
  ticketId: string;
  loop: Tdm79LoopState;
  liveUpdatedAtMicros?: bigint;
  onClose: () => void;
}) {
  const [base, setBase] = useState<Tdm79Settings>();
  const [form, setForm] = useState<Tdm79Form>();
  const [loadError, setLoadError] = useState<string>();
  const [errors, setErrors] = useState<Tdm79Errors>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string>();
  const [name, setName] = useState('');
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();

  const load = useCallback(() => {
    setLoadError(undefined);
    fetchSettings(ticketId)
      .then((s) => {
        setBase(s);
        setForm(toForm(s));
        setErrors({});
      })
      .catch((e: Error) => setLoadError(e.message));
  }, [ticketId]);

  useEffect(() => {
    setName(readName());
    load();
  }, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    panel.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const live = useMemo(() => {
    const iteration = base?.ticket.iteration ?? loop.iteration;
    return { ...loop, status: base?.ticket.status ?? loop.status, iteration, maxIterations: base?.max_iterations ?? loop.maxIterations };
  }, [base, loop]);
  const notes = pickupNotes(live);
  const frozen = TDM79_TERMINAL.includes(live.status);
  const staleElsewhere =
    base && liveUpdatedAtMicros !== undefined && liveUpdatedAtMicros > BigInt(base.updated_at_micros) && !saving;

  const { patch, errors: localErrors } = useMemo(
    () => (form && base ? buildPatch(form, base) : { patch: {}, errors: {} }),
    [form, base],
  );
  const changes = Object.keys(patch).length + (patch.hyperparams ? Object.keys(patch.hyperparams as object).length - 1 : 0);
  const shown: Tdm79Errors = { ...errors, ...localErrors };
  const nameOk = name === '' || HUMAN_ID.test(name);

  const set = <K extends Tdm79Field>(k: K, v: Tdm79Form[K]) => {
    setForm((f) => (f ? { ...f, [k]: v } : f));
    setSaved(undefined);
    setErrors((e) => {
      const { [k]: _drop, form: _f, ...rest } = e;
      return rest;
    });
  };

  async function save() {
    if (!base || !form || !changes || Object.keys(localErrors).length || !nameOk) return;
    setSaving(true);
    setSaved(undefined);
    if (name) saveName(name);
    const r = await patchSettings(ticketId, patch, name || 'web-dashboard');
    setSaving(false);
    if (r.ok) {
      setBase(r.settings);
      setForm(toForm(r.settings));
      setErrors({});
      setSaved(
        `Saved ${changes} change${changes === 1 ? '' : 's'}, logged as human:${name || 'web-dashboard'}` +
          (r.settings.audit_logged === false ? ' (the audit event did not write; the settings did).' : '.'),
      );
    } else {
      setErrors(r.errors);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end print:hidden" role="presentation">
      <button type="button" aria-label="Close run settings" className="absolute inset-0 bg-zinc-950/30 backdrop-blur-[1px]" onClick={onClose} />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative flex h-full w-full max-w-md flex-col bg-white shadow-2xl outline-none dark:bg-zinc-900"
      >
        <div className="flex items-start justify-between gap-4 border-b border-zinc-200 px-5 py-4 dark:border-zinc-800">
          <div>
            <h2 id={titleId} className="text-lg font-semibold tracking-tight">
              Run settings
            </h2>
            <p className="mt-0.5 text-sm text-zinc-500">
              Ticket #{ticketId}
              {base && (
                <>
                  , iteration {base.ticket.iteration} of {base.max_iterations}, {base.ticket.status.replace(/_/g, ' ')}
                </>
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-blue-600 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            aria-label="Close"
          >
            <svg aria-hidden viewBox="0 0 16 16" className="h-5 w-5 stroke-current" strokeWidth="1.5">
              <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {loadError && (
            <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
              <p>Couldn&apos;t load the settings: {loadError}</p>
              <button type="button" onClick={load} className="mt-1 font-medium underline">
                Try again
              </button>
            </div>
          )}
          {!form && !loadError && <p className="text-sm text-zinc-500">Loading settings…</p>}
          {frozen && (
            <p className="mb-4 rounded-md border-l-4 border-zinc-400 bg-zinc-100 px-3 py-2 text-sm text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
              This ticket is {live.status}. Its settings are frozen and shown read-only.
            </p>
          )}
          {staleElsewhere && (
            <p className="mb-4 rounded-md border-l-4 border-amber-500 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
              Someone else changed these settings since you opened them.{' '}
              <button type="button" onClick={load} className="font-medium underline">
                Load their version
              </button>
            </p>
          )}

          {/* TDM-107: sim robot or real YAM, switchable by an operator; include_sim_data. */}
          {base && (
            <Tdm107RobotModePanel
              ticketId={ticketId}
              humanId={name && HUMAN_ID.test(name) ? name : 'web-dashboard'}
              frozen={frozen}
              onSettingsSaved={(s) => setBase(s)}
            />
          )}

          {form && base && (
            <fieldset disabled={frozen || saving} className="space-y-7">
              <Group title="Evaluation" when={notes.eval}>
                <NumField label="Trials per eval" field="eval_trials" form={form} set={set} err={shown.eval_trials} hint="1 to 50" />
              </Group>

              <Group title="Ship rule" when={notes.verdict}>
                <NumField label="Success threshold" field="success_pct" form={form} set={set} err={shown.success_pct} hint="0 to 100" suffix="%" step="1" />
                <label className="flex items-start gap-3 text-sm">
                  <input
                    type="checkbox"
                    checked={form.require_ci_low}
                    onChange={(e) => set('require_ci_low', e.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-zinc-300 accent-blue-600"
                  />
                  <span>
                    <span className="font-medium">Ship only when the confidence interval clears it</span>
                    <span className="block text-zinc-500">
                      Uses the lower bound of the 95% interval instead of the measured rate. Safer with 10 trials.
                    </span>
                  </span>
                </label>
                <FieldError msg={shown.require_ci_low} />
                <NumField
                  label="Max iterations"
                  field="max_iterations"
                  form={form}
                  set={set}
                  err={shown.max_iterations}
                  hint={`${base.ticket.iteration + 1} to 20; more than the current iteration`}
                />
              </Group>

              <Group title="Data" when={notes.data}>
                <NumField label="Target accepted episodes" field="target_episodes" form={form} set={set} err={shown.target_episodes} hint="1 to 10,000" />
                <div>
                  <label className="block text-sm font-medium" htmlFor="tdm79-dto">
                    Data type
                  </label>
                  <select
                    id="tdm79-dto"
                    value={form.data_type_override}
                    onChange={(e) => set('data_type_override', e.target.value as Tdm79Form['data_type_override'])}
                    className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-950"
                  >
                    <option value="">Let the planner decide</option>
                    <option value="clean_teleop">Always clean teleop demos</option>
                    <option value="recovery_takeover">Always recovery takeovers</option>
                  </select>
                  <FieldError msg={shown.data_type_override} />
                  {form.data_type_override && (
                    <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
                      Known gap: the planner reads its override from hyperparams or a planner.data_type_override event, not
                      this saved field, so check the next data plan&apos;s guardrail line before relying on it.
                    </p>
                  )}
                </div>
              </Group>

              <Group title="Budget" when={notes.budget}>
                <NumField
                  label="Budget"
                  field="budget_usd"
                  form={form}
                  set={set}
                  err={shown.budget_usd}
                  prefix="$"
                  step="0.01"
                  hint={`At least $${(Math.max(base.ticket.approved_cents, base.ticket.spent_cents) / 100).toFixed(2)}, already approved or spent. Stripe test mode.`}
                />
              </Group>

              <Group title="Training" when={notes.training}>
                <div className="grid grid-cols-2 gap-3">
                  <NumField label="Learning rate" field="lr" form={form} set={set} err={shown.lr} hint="above 0, at most 1" inputMode="decimal" step="any" />
                  <NumField label="Steps" field="steps" form={form} set={set} err={shown.steps} hint="1 to 1,000,000" />
                  <NumField label="Batch size" field="batch_size" form={form} set={set} err={shown.batch_size} hint="1 to 4,096" />
                  <NumField label="LoRA rank" field="lora_rank" form={form} set={set} err={shown.lora_rank} hint="0 = full fine-tune" />
                </div>
                <div>
                  <label className="block text-sm font-medium" htmlFor="tdm79-prompt">
                    Policy prompt
                  </label>
                  <textarea
                    id="tdm79-prompt"
                    rows={2}
                    value={form.prompt}
                    onChange={(e) => set('prompt', e.target.value)}
                    className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-950"
                    placeholder="The instruction the policy is trained and run with"
                  />
                  <FieldError msg={shown.prompt} />
                </div>
              </Group>
            </fieldset>
          )}
        </div>

        {form && base && !frozen && (
          <div className="border-t border-zinc-200 px-5 py-4 dark:border-zinc-800">
            {shown.form && (
              <p role="alert" className="mb-3 text-sm text-red-700 dark:text-red-400">
                {shown.form}
              </p>
            )}
            {saved && (
              <p role="status" className="mb-3 text-sm text-emerald-700 dark:text-emerald-400">
                {saved}
              </p>
            )}
            <div className="flex items-end gap-3">
              <div className="min-w-0 flex-1">
                <label htmlFor="tdm79-name" className="block text-xs text-zinc-500">
                  Your name, for the event log
                </label>
                <input
                  id="tdm79-name"
                  value={name}
                  onChange={(e) => setName(e.target.value.trim())}
                  placeholder="web-dashboard"
                  className={`mt-0.5 w-full rounded-md border bg-white px-2 py-1 text-sm dark:bg-zinc-950 ${nameOk ? 'border-zinc-300 dark:border-zinc-700' : 'border-red-500'}`}
                />
              </div>
              <button
                type="button"
                onClick={() => form && base && setForm(toForm(base))}
                disabled={!changes || saving}
                className="rounded-md px-3 py-1.5 text-sm text-zinc-600 hover:text-zinc-900 disabled:opacity-40 dark:text-zinc-400 dark:hover:text-zinc-100"
              >
                Reset
              </button>
              <button
                type="button"
                onClick={save}
                disabled={!changes || saving || Object.keys(localErrors).length > 0 || !nameOk}
                className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:bg-zinc-300 disabled:text-zinc-600 dark:disabled:bg-zinc-700 dark:disabled:text-zinc-400"
              >
                {saving
                  ? 'Saving…'
                  : Object.keys(localErrors).length
                    ? `Fix ${Object.keys(localErrors).length} field${Object.keys(localErrors).length === 1 ? '' : 's'}`
                    : changes
                      ? `Save ${changes} change${changes === 1 ? '' : 's'}`
                      : 'No changes'}
              </button>
            </div>
            {!nameOk && <p className="mt-1 text-xs text-red-600">Letters, digits and . _ @ : + - only, up to 64.</p>}
          </div>
        )}
      </div>
    </div>
  );
}

function Group({ title, when, children }: { title: string; when: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mb-3 mt-1 border-l-2 border-blue-500/60 pl-2 text-xs text-zinc-600 dark:text-zinc-400">
        <span className="font-medium text-zinc-800 dark:text-zinc-200">Takes effect: </span>
        {when}
      </p>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function FieldError({ msg }: { msg?: string }) {
  if (!msg) return null;
  return (
    <p role="alert" className="mt-1 text-xs text-red-700 dark:text-red-400">
      {msg}
    </p>
  );
}

function NumField({
  label,
  field,
  form,
  set,
  err,
  hint,
  prefix,
  suffix,
  step = '1',
  inputMode = 'numeric',
}: {
  label: string;
  field: Tdm79Field;
  form: Tdm79Form;
  set: (k: Tdm79Field, v: string) => void;
  err?: string;
  hint?: string;
  prefix?: string;
  suffix?: string;
  step?: string;
  inputMode?: 'numeric' | 'decimal';
}) {
  const id = `tdm79-${field}`;
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <div
        className={`mt-1 flex items-center rounded-md border bg-white text-sm focus-within:ring-2 focus-within:ring-blue-500/40 dark:bg-zinc-950 ${
          err ? 'border-red-500' : 'border-zinc-300 dark:border-zinc-700'
        }`}
      >
        {prefix && <span className="pl-2.5 text-zinc-500">{prefix}</span>}
        <input
          id={id}
          value={String(form[field])}
          onChange={(e) => set(field, e.target.value)}
          inputMode={inputMode === 'decimal' || step !== '1' ? 'decimal' : 'numeric'}
          data-step={step}
          aria-invalid={Boolean(err)}
          aria-describedby={`${id}-hint`}
          className="w-full min-w-0 rounded-md bg-transparent px-2.5 py-1.5 tabular-nums outline-none"
        />
        {suffix && <span className="pr-2.5 text-zinc-500">{suffix}</span>}
      </div>
      {err ? (
        <FieldError msg={err} />
      ) : (
        hint && (
          <p id={`${id}-hint`} className="mt-1 text-xs text-zinc-500">
            {hint}
          </p>
        )
      )}
    </div>
  );
}
