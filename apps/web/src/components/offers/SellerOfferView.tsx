'use client';

// TDM-94: the dataset seller's page for one offer (/sell/[offerId]).
//
// The seller (a contributor of kind dataset_seller, TDM-60; this browser's own identity) sees what
// the factory is buying and the cap, accepts the offer (accept_dataset_offer as itself), uploads
// episodes straight to signed storage URLs with progress, submits the manifest, marks delivery done
// and watches per-episode QA verdicts and the payout land live. POSTs go through /api/tdm94, which
// checks this browser's identity is the offer's seller. Stripe TEST MODE: no real money moves.

import Link from 'next/link';
import { useCallback, useMemo, useRef, useState } from 'react';
import { useReducer } from 'spacetimedb/react';
import { reducers } from '@/module_bindings';
import { useTicket } from '@/lib/spacetime/hooks';
import { useContributor } from '@/lib/contributor/hooks';
import { fmtCents } from '@/lib/format';
import { StatusPill } from '@/components/ticket/Badges';
import { EpisodeVerdicts, OfferCounts, PayoutStatus, QaFlags, SlotRail, type UploadSlot } from './OfferParts';
import { INGESTABLE, neededArithmetic, OFFER_STATUS_TEXT, type OfferView } from '@/lib/tdm94/offers';
import { useOfferLive } from '@/lib/tdm94/useOfferLive';
import {
  cameraName,
  episodeTiming,
  filesFromDrop,
  filesFromInput,
  offerPost,
  planDelivery,
  putSigned,
  type DeliveryPlan,
  type IngestResponse,
  type PickedFile,
  type SignResponse,
  type SignedFile,
  type Timing,
} from '@/lib/tdm94/upload';

export function SellerOfferView({ offerId }: { offerId: string }) {
  const oid = useMemo(() => {
    try {
      return /^\d+$/.test(offerId) ? BigInt(offerId) : -1n;
    } catch {
      return -1n;
    }
  }, [offerId]);
  const [view, ready, offer] = useOfferLive(oid < 0n ? 0n : oid);
  const [ticket] = useTicket(offer?.ticketId ?? 0n);
  const { contributor, ready: meReady } = useContributor();

  if (oid < 0n) return <Note>“{offerId}” is not an offer number.</Note>;
  if (!ready) return <p className="text-sm text-zinc-500">Connecting…</p>;
  if (!offer || !view) return <Note>There is no dataset offer {offerId} in this database.</Note>;

  const isSeller = !!contributor && contributor.id === offer.sellerId;
  return (
    <div className="mx-auto max-w-3xl">
      <SellerHeader view={view} task={ticket?.taskDescription} />
      <div className="mt-6">
        <SlotRailWithUploads view={view} isSeller={isSeller} meReady={meReady} contributorName={contributor?.name} />
      </div>
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="mx-auto max-w-3xl rounded-md border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-500 dark:border-zinc-700">{children}</p>;
}

function SellerHeader({ view: v, task }: { view: OfferView; task?: string }) {
  const o = v.offer;
  return (
    <header>
      <p className="flex flex-wrap items-center gap-2 text-sm text-zinc-500">
        <span>
          Dataset offer {o.id.toString()} for ticket {o.ticketId.toString()}
        </span>
        <StatusPill status={o.status} />
        <span>{OFFER_STATUS_TEXT[o.status] ?? ''}</span>
      </p>
      <h1 className="mt-2 text-2xl font-semibold leading-snug tracking-tight sm:text-3xl">{task ? `“${task}”` : 'Robot episodes'}</h1>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <Term label="Buying">
          <span className="text-3xl font-semibold tabular-nums">{o.neededCount}</span>{' '}
          <span className="text-sm">episode{o.neededCount === 1 ? '' : 's'}</span>
          <span className="mt-1 block text-xs text-zinc-500">{neededArithmetic(v)}</span>
        </Term>
        <Term label="Price per accepted episode">
          <span className="text-3xl font-semibold tabular-nums">{fmtCents(o.pricePerEpisodeCents)}</span>
          <span className="mt-1 block text-xs text-zinc-500">Paid only after QA accepts it</span>
        </Term>
        <Term label="Most this offer pays">
          <span className="text-3xl font-semibold tabular-nums">{fmtCents(v.maxSpendCents)}</span>
          <span className="mt-1 block text-xs text-zinc-500">
            {o.neededCount} × {fmtCents(o.pricePerEpisodeCents)}; extra episodes are not bought
          </span>
        </Term>
      </div>
      {o.sourceUri && (
        <p className="mt-3 text-sm text-zinc-500">
          Source dataset: <code>{o.sourceUri}</code>
        </p>
      )}
    </header>
  );
}

function Term({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-zinc-200 bg-white px-3 py-2.5 dark:border-zinc-800 dark:bg-zinc-900">
      <p className="text-xs text-zinc-500">{label}</p>
      <div className="mt-0.5">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------- the working area

type Staged = {
  plan: DeliveryPlan;
  timings: Map<string, Timing | Error>; // raw: ref -> timing
};

type Phase =
  | { kind: 'idle' }
  | { kind: 'reading' }
  | { kind: 'staged' }
  | { kind: 'signing' }
  | { kind: 'uploading' }
  | { kind: 'submitting' }
  | { kind: 'done' };

type Outcome = {
  ingested: string[];
  already: string[];
  refused: { ref: string; reason: string }[];
  uploadErrors: { ref: string; reason: string }[];
};

function SlotRailWithUploads({ view, isSeller, meReady, contributorName }: { view: OfferView; isSeller: boolean; meReady: boolean; contributorName?: string }) {
  const o = view.offer;
  const accept = useReducer(reducers.acceptDatasetOffer);
  const [staged, setStaged] = useState<Staged | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const [progress, setProgress] = useState<Map<string, { sent: number; total: number; failed?: boolean }>>(new Map());
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const busyPhase = ['reading', 'signing', 'uploading', 'submitting'].includes(phase.kind);

  const uploads: UploadSlot[] = useMemo(
    () =>
      phase.kind === 'uploading' || phase.kind === 'submitting'
        ? [...progress.entries()].map(([ref, p]) => ({ ref, pct: p.total ? Math.floor((p.sent / p.total) * 100) : 0, failed: p.failed }))
        : [],
    [phase.kind, progress],
  );

  const stage = useCallback(async (picked: PickedFile[]) => {
    setError(null);
    setOutcome(null);
    if (picked.length === 0) return;
    setPhase({ kind: 'reading' });
    const plan = planDelivery(picked);
    const timings = new Map<string, Timing | Error>();
    if (plan.format === 'raw') {
      const seen = new Set<string>();
      for (const ep of plan.episodes) {
        let ref = ep.ref;
        for (let i = 2; seen.has(ref); i++) ref = `${ep.ref}-${i}`;
        ep.ref = ref;
        seen.add(ref);
        if (ep.problems.length) continue;
        try {
          timings.set(ep.ref, await episodeTiming(ep));
        } catch (e) {
          timings.set(ep.ref, e as Error);
        }
      }
    }
    setStaged({ plan, timings });
    setPhase({ kind: 'staged' });
  }, []);

  const send = useCallback(async () => {
    if (!staged) return;
    setError(null);
    const out: Outcome = { ingested: [], already: [], refused: [], uploadErrors: [] };
    const prog = new Map<string, { sent: number; total: number; failed?: boolean }>();
    const bump = () => setProgress(new Map(prog));
    try {
      if (staged.plan.format === 'raw') {
        const ready = staged.plan.episodes.filter((e) => !e.problems.length && staged.timings.get(e.ref) && !(staged.timings.get(e.ref) instanceof Error));
        if (ready.length === 0) throw new Error('No episode is ready to upload.');
        setPhase({ kind: 'signing' });
        const sign = await offerPost<SignResponse>(o.id, 'uploads/sign', {
          episodes: ready.map((e) => ({
            ref: e.ref,
            files: [...e.cameras.map((c) => ({ filename: `${cameraName(c)}.mp4`, content_type: 'video/mp4' })), { filename: 'actions.parquet', content_type: 'application/octet-stream' }],
          })),
        });
        for (const r of sign.refused) out.refused.push({ ref: r.ref ?? r.file ?? '?', reason: r.reason });
        const signed = sign.uploads.filter((u): u is { ref: string; files: SignedFile[] } => 'ref' in u && 'files' in u);
        const byRef = new Map(ready.map((e) => [e.ref, e]));
        for (const u of signed) {
          const ep = byRef.get(u.ref)!;
          prog.set(u.ref, { sent: 0, total: [...ep.cameras, ep.actions!].reduce((s, f) => s + f.file.size, 0) });
        }
        bump();
        setPhase({ kind: 'uploading' });
        const okRefs: string[] = [];
        await Promise.all(
          signed.map(async (u) => {
            const ep = byRef.get(u.ref)!;
            const local = new Map<string, File>([...ep.cameras.map((c) => [`${cameraName(c)}.mp4`, c.file] as const), ['actions.parquet', ep.actions!.file]]);
            const sent = new Map<string, number>();
            try {
              for (const f of u.files) {
                const file = local.get(f.filename ?? '');
                if (!file) throw new Error(`no local file for ${f.filename}`);
                await putSigned(f.upload_url, f.headers, file, (n) => {
                  sent.set(f.filename!, n);
                  prog.get(u.ref)!.sent = [...sent.values()].reduce((s, x) => s + x, 0);
                  bump();
                });
              }
              okRefs.push(u.ref);
            } catch (e) {
              prog.get(u.ref)!.failed = true;
              bump();
              out.uploadErrors.push({ ref: u.ref, reason: (e as Error).message });
            }
          }),
        );
        if (okRefs.length) {
          setPhase({ kind: 'submitting' });
          const manifest = {
            format: 'raw',
            episodes: okRefs.map((ref) => {
              const ep = byRef.get(ref)!;
              const t = staged.timings.get(ref) as Timing;
              return {
                ref,
                cameras: Object.fromEntries(ep.cameras.map((c) => [cameraName(c), `${cameraName(c)}.mp4`])),
                actions: 'actions.parquet',
                frame_count: t.frameCount,
                duration_ms: t.durationMs,
              };
            }),
          };
          const ing = await offerPost<IngestResponse>(o.id, 'episodes', manifest);
          collect(out, ing);
        }
      } else {
        const plan = staged.plan;
        if (plan.problems.length) throw new Error(plan.problems.join('; '));
        setPhase({ kind: 'signing' });
        const sign = await offerPost<SignResponse>(o.id, 'uploads/sign', { lerobot_files: plan.files.map((f) => f.rel) });
        for (const r of sign.refused) out.refused.push({ ref: r.file ?? r.ref ?? '?', reason: r.reason });
        const files = new Map(plan.files.map((f) => [f.rel, f.file]));
        const signed = sign.uploads.filter((u): u is SignedFile => 'upload_url' in u);
        const total = signed.reduce((s, u) => s + (files.get(u.file ?? '')?.size ?? 0), 0);
        prog.set(plan.root, { sent: 0, total });
        bump();
        setPhase({ kind: 'uploading' });
        const sent = new Map<string, number>();
        for (const u of signed) {
          const file = files.get(u.file ?? '');
          if (!file) continue;
          await putSigned(u.upload_url, u.headers, file, (n) => {
            sent.set(u.file!, n);
            prog.get(plan.root)!.sent = [...sent.values()].reduce((s, x) => s + x, 0);
            bump();
          });
        }
        setPhase({ kind: 'submitting' });
        const ing = await offerPost<IngestResponse>(o.id, 'episodes', { format: 'lerobot' });
        collect(out, ing);
      }
      setOutcome(out);
      setStaged(null);
      setPhase({ kind: 'done' });
    } catch (e) {
      setError((e as Error).message);
      setOutcome(out.refused.length || out.ingested.length ? out : null);
      setPhase({ kind: staged ? 'staged' : 'idle' });
    }
  }, [staged, o.id]);

  const markDelivered = useCallback(async () => {
    setBusy('delivered');
    setError(null);
    try {
      await offerPost(o.id, 'delivered');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }, [o.id]);

  const doAccept = useCallback(async () => {
    setBusy('accept');
    setError(null);
    try {
      await accept({ offerId: o.id });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [accept, o.id]);

  const ingestable = INGESTABLE.includes(o.status);
  const inputFolders = useRef<HTMLInputElement>(null);
  const inputFiles = useRef<HTMLInputElement>(null);

  return (
    <>
      <section aria-labelledby="slots-h">
        <div className="mb-2 flex flex-wrap items-baseline gap-x-3">
          <h2 id="slots-h" className="text-lg font-semibold tracking-tight">
            Slots
          </h2>
          <span className="text-sm tabular-nums text-zinc-500">
            {view.openSlots} of {o.neededCount} open
          </span>
        </div>
        <SlotRail view={view} uploads={uploads} />
      </section>

      {!isSeller && (
        <p className="mt-6 rounded-md border border-zinc-300 bg-white px-4 py-3 text-sm dark:border-zinc-700 dark:bg-zinc-900">
          {meReady
            ? `You are viewing this offer${contributorName ? ` as ${contributorName}` : ''}. Only the seller it was made to can accept it and upload episodes.`
            : 'Connecting your identity…'}{' '}
          Selling a dataset? <Link href="/join" className="font-medium text-blue-700 underline dark:text-blue-400">Sign up as a dataset seller</Link>.
        </p>
      )}

      {isSeller && o.status === 'proposed' && (
        <section className="mt-6 rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
          <p className="text-sm">
            Accept to start uploading. You will be paid {fmtCents(o.pricePerEpisodeCents)} for each episode QA accepts, for up to {o.neededCount} episodes.
          </p>
          <button
            type="button"
            onClick={doAccept}
            disabled={busy === 'accept'}
            className="mt-3 rounded-md bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {busy === 'accept' ? 'Accepting…' : 'Accept offer'}
          </button>
        </section>
      )}

      {isSeller && ingestable && (
        <section aria-labelledby="up-h" className="mt-6">
          <h2 id="up-h" className="mb-2 text-lg font-semibold tracking-tight">
            Upload episodes
          </h2>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={async (e) => {
              e.preventDefault();
              setDragOver(false);
              if (busyPhase) return;
              await stage(await filesFromDrop(e.dataTransfer));
            }}
            className={`rounded-lg border-2 border-dashed px-4 py-6 text-center transition-colors ${
              dragOver ? 'border-sky-500 bg-sky-50 dark:bg-sky-950/30' : 'border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900'
            }`}
          >
            <p className="text-sm font-medium">Drop episode folders here</p>
            <p className="mx-auto mt-1 max-w-md text-xs text-zinc-500">
              One folder per episode with a <code>.mp4</code> per camera (named by camera, e.g. <code>exo.mp4</code>) and <code>actions.parquet</code>, or one
              LeRobot v2.1 dataset folder. {view.openSlots} slot{view.openSlots === 1 ? '' : 's'} open; anything past that is not bought.
            </p>
            <div className="mt-3 flex flex-wrap justify-center gap-2">
              <button type="button" disabled={busyPhase} onClick={() => inputFolders.current?.click()} className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-600 dark:hover:bg-zinc-800">
                Choose folders
              </button>
              <button type="button" disabled={busyPhase} onClick={() => inputFiles.current?.click()} className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-600 dark:hover:bg-zinc-800">
                Choose files
              </button>
            </div>
            <input
              ref={inputFolders}
              type="file"
              multiple
              className="hidden"
              {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
              onChange={(e) => {
                void stage(filesFromInput(e.target.files));
                e.target.value = '';
              }}
            />
            <input
              ref={inputFiles}
              type="file"
              multiple
              accept=".mp4,.parquet,video/mp4"
              className="hidden"
              onChange={(e) => {
                void stage(filesFromInput(e.target.files));
                e.target.value = '';
              }}
            />
          </div>

          {phase.kind === 'reading' && <p className="mt-3 text-sm text-zinc-500">Reading the files…</p>}
          {staged && <StagedList staged={staged} openSlots={view.openSlots} />}
          {staged && (
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={send}
                disabled={busyPhase}
                className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                {phase.kind === 'signing'
                  ? 'Getting upload links…'
                  : phase.kind === 'uploading'
                    ? 'Uploading…'
                    : phase.kind === 'submitting'
                      ? 'Submitting…'
                      : `Upload and submit`}
              </button>
              <button type="button" disabled={busyPhase} onClick={() => (setStaged(null), setPhase({ kind: 'idle' }))} className="text-sm text-zinc-600 hover:underline disabled:opacity-50 dark:text-zinc-400">
                Clear
              </button>
            </div>
          )}
          {outcome && <OutcomeList outcome={outcome} />}

          <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-zinc-200 pt-4 dark:border-zinc-800">
            {view.deliveryComplete ? (
              <p className="text-sm text-zinc-600 dark:text-zinc-400">You marked delivery as complete. The offer settles once every episode has a verdict.</p>
            ) : (
              <>
                <button
                  type="button"
                  onClick={markDelivered}
                  disabled={busy === 'delivered' || view.live === 0 || busyPhase}
                  className="rounded-md border border-zinc-400 px-4 py-2 text-sm font-semibold hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-600 dark:hover:bg-zinc-800"
                >
                  {busy === 'delivered' ? 'Saving…' : 'Mark delivery complete'}
                </button>
                <p className="text-xs text-zinc-500">Tell us you have nothing more to send, so we settle on what QA accepted instead of waiting for open slots.</p>
              </>
            )}
          </div>
        </section>
      )}

      {error && (
        <p role="alert" className="mt-4 rounded-md border-l-4 border-red-600 bg-red-50 px-3 py-2 text-sm text-red-900 dark:bg-red-950/40 dark:text-red-200">
          {error}
        </p>
      )}

      {(o.status === 'done' || o.status === 'cancelled') && (
        <p className="mt-6 text-sm text-zinc-600 dark:text-zinc-400">
          {o.status === 'done' ? 'This offer is settled; it takes no more uploads.' : 'This offer was cancelled; it takes no more uploads.'}
        </p>
      )}

      <section className="mt-8" aria-labelledby="qa-h">
        <h2 id="qa-h" className="mb-2 text-lg font-semibold tracking-tight">
          Review results
        </h2>
        <div className="space-y-3">
          <OfferCounts view={view} />
          <QaFlags view={view} />
          <EpisodeVerdicts view={view} priceCents={o.pricePerEpisodeCents} />
        </div>
      </section>

      <section className="mt-8 mb-12" aria-labelledby="pay-h">
        <h2 id="pay-h" className="mb-2 text-lg font-semibold tracking-tight">
          Payout
        </h2>
        <PayoutStatus view={view} />
      </section>
    </>
  );
}

function collect(out: Outcome, ing: IngestResponse) {
  out.ingested.push(...ing.ingested.map((x) => x.ref));
  out.already.push(...ing.already_ingested.map((x) => x.ref));
  out.refused.push(...ing.refused.map((x) => ({ ref: x.ref, reason: x.reason })));
}

function StagedList({ staged, openSlots }: { staged: Staged; openSlots: number }) {
  const p = staged.plan;
  if (p.format === 'lerobot') {
    return (
      <div className="mt-3 rounded-md border border-zinc-200 bg-white p-3 text-sm dark:border-zinc-800 dark:bg-zinc-900">
        <p>
          LeRobot dataset <code>{p.root}</code>: {p.episodeIndexes.length} episode{p.episodeIndexes.length === 1 ? '' : 's'}, {p.files.length} files.
          {p.episodeIndexes.length > openSlots && ` Only the first ${openSlots} get upload links.`}
        </p>
        {p.problems.map((x) => (
          <p key={x} className="mt-1 text-red-700 dark:text-red-400">
            {x}
          </p>
        ))}
      </div>
    );
  }
  let usable = 0;
  return (
    <div className="mt-3">
      <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 bg-white text-sm dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
        {p.episodes.map((e) => {
          const t = staged.timings.get(e.ref);
          const bad = e.problems.length > 0 || !t || t instanceof Error;
          const over = !bad && ++usable > openSlots;
          return (
            <li key={e.ref} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2">
              <span className="font-medium">{e.ref}</span>
              <span className="text-xs text-zinc-500">{e.cameras.map(cameraName).join(', ') || 'no cameras'}</span>
              {t && !(t instanceof Error) && (
                <span className="text-xs tabular-nums text-zinc-500">
                  {(t.durationMs / 1000).toFixed(1)} s, {t.source}
                  {t.fixture && <span className="ml-2 rounded-sm bg-fuchsia-600 px-1 text-[10px] font-bold text-white">FIXTURE</span>}
                </span>
              )}
              <span className="ml-auto text-xs">
                {bad ? (
                  <span className="text-red-700 dark:text-red-400">{e.problems.join('; ') || (t instanceof Error ? t.message : 'unreadable')}</span>
                ) : over ? (
                  <span className="text-amber-700 dark:text-amber-400">past the open slots; the server will refuse it</span>
                ) : (
                  <span className="text-emerald-700 dark:text-emerald-400">ready</span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {p.ignored.length > 0 && <p className="mt-1 text-xs text-zinc-500">Ignored {p.ignored.length} other file(s): {p.ignored.slice(0, 4).join(', ')}{p.ignored.length > 4 ? '…' : ''}</p>}
    </div>
  );
}

function OutcomeList({ outcome }: { outcome: Outcome }) {
  return (
    <div className="mt-3 space-y-1 text-sm" role="status">
      {outcome.ingested.length > 0 && (
        <p className="text-emerald-800 dark:text-emerald-300">
          Submitted {outcome.ingested.length}: {outcome.ingested.join(', ')}. QA starts now.
        </p>
      )}
      {outcome.already.length > 0 && <p className="text-zinc-600 dark:text-zinc-400">Already submitted: {outcome.already.join(', ')}.</p>}
      {outcome.refused.map((r) => (
        <p key={r.ref + r.reason} className="text-amber-900 dark:text-amber-300">
          <span className="font-medium">{r.ref}</span>: {r.reason}
        </p>
      ))}
      {outcome.uploadErrors.map((r) => (
        <p key={r.ref} className="text-red-800 dark:text-red-300">
          <span className="font-medium">{r.ref}</span> upload failed: {r.reason}
        </p>
      ))}
    </div>
  );
}
