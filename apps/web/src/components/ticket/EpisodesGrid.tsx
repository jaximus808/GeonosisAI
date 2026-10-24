'use client';

import { useEffect, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents, fmtTs } from '@/lib/format';
import { Empty, FakeBadge, SimBadge, StatusPill } from './Badges';
import { CamVideo } from './media';
import {
  KIND_LABEL,
  byNewest,
  cameraUris,
  fmtDuration,
  isFakeEpisode,
  parseObj,
  shortUri,
  type Contributor,
  type Episode,
  type Payment,
  type Review,
} from './util';

const COLLECTED = new Set(['clean_teleop', 'recovery_takeover', 'purchased']);
const FILTERS = [
  { key: 'collected', label: 'Collected data', test: (e: Episode) => COLLECTED.has(e.kind) },
  { key: 'rollouts', label: 'Eval rollouts', test: (e: Episode) => !COLLECTED.has(e.kind) },
  { key: 'all', label: 'All', test: () => true },
] as const;
type FilterKey = (typeof FILTERS)[number]['key'];

function QaChecks({ episodeId }: { episodeId: bigint }) {
  const [rows] = useRows('qaResult', (r) => r.episodeId.eq(episodeId));
  if (rows.length === 0) return null;
  return (
    <ul className="mt-2 flex flex-wrap gap-1">
      {rows.map((q) => {
        const d = parseObj(q.detailJson);
        const detail = d && typeof d.value === 'number' && typeof d.threshold === 'number' ? ` ${d.value} / ${d.threshold}` : '';
        return (
          <li
            key={q.id.toString()}
            title={q.detailJson}
            className={`rounded px-1.5 py-0.5 text-[11px] ring-1 ring-inset ${q.passed ? 'text-emerald-800 ring-emerald-600/30 dark:text-emerald-300' : 'bg-red-50 text-red-800 ring-red-600/40 dark:bg-red-950/40 dark:text-red-300'}`}
          >
            {q.passed ? 'pass' : 'fail'} {q.checkName.replace(/_/g, ' ')}{detail}
          </li>
        );
      })}
    </ul>
  );
}

function Verdict({ e, review }: { e: Episode; review?: Review }) {
  if (review) {
    return (
      <div
        className={`mt-3 rounded px-3 py-2 text-sm ${review.accepted ? 'bg-emerald-50 text-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200' : 'bg-red-50 text-red-900 dark:bg-red-950/30 dark:text-red-200'}`}
      >
        <p className="font-semibold">{review.accepted ? 'Accepted' : 'Rejected'}<span className="ml-1 font-normal opacity-70">by {review.reviewer}</span></p>
        {review.reason && <p className="mt-0.5">{review.reason}</p>}
      </div>
    );
  }
  if (e.reviewStatus === 'pending') return <p className="mt-3 text-sm text-zinc-500">Waiting for review.</p>;
  if (e.reviewStatus === 'not_reviewed') return null;
  return <p className="mt-3"><StatusPill status={e.reviewStatus} /></p>;
}

function EpisodeCard({ e, review, payment, who, sim }: { e: Episode; review?: Review; payment?: Payment; who?: Contributor; sim?: boolean }) {
  const fake = isFakeEpisode(e);
  const cams = cameraUris(e);
  return (
    <article id={`episode-${e.id}`} className={`scroll-mt-6 rounded-lg border bg-white p-3 target:ring-2 target:ring-sky-500 dark:bg-zinc-900 ${fake ? 'border-fuchsia-400 dark:border-fuchsia-700' : 'border-zinc-200 dark:border-zinc-800'}`}>
      <header className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold tabular-nums">#{e.id.toString()}</span>
        <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs dark:bg-zinc-800">{KIND_LABEL[e.kind] ?? e.kind}</span>
        {fake && <FakeBadge />}
        {sim && <SimBadge />}
        {e.success !== undefined && (
          <span className={`text-xs font-medium ${e.success ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-700 dark:text-red-400'}`}>
            {e.success ? 'task succeeded' : 'task failed'}
          </span>
        )}
        <span className="ml-auto whitespace-nowrap text-xs text-zinc-500" title={fmtTs(e.createdAt)}>{e.createdAt.toDate().toLocaleTimeString()}</span>
      </header>
      {cams.length > 0 ? (
        <div className={`grid gap-2 ${cams.length > 1 ? 'grid-cols-2' : 'grid-cols-1'}`}>
          {cams.map((c) => <CamVideo key={c.name} uri={c.uri} name={c.name} fake={fake} />)}
        </div>
      ) : (
        <p className="rounded bg-zinc-100 p-3 text-xs text-zinc-500 dark:bg-zinc-800">No camera files recorded.</p>
      )}
      <p className="mt-2 text-xs tabular-nums text-zinc-500">
        {fmtDuration(e.durationMs)}, {e.frameCount} frames
        {who && <>, by {who.name}</>}
        {e.takeoverTs && (
          <>, takeover {e.takeoverTs.toDate().toLocaleTimeString()}{e.handbackTs ? ` to ${e.handbackTs.toDate().toLocaleTimeString()}` : ', no handback logged'}</>
        )}
        {e.checkpointUri && <>, policy <code title={e.checkpointUri}>{shortUri(e.checkpointUri, 2)}</code></>}
      </p>
      <Verdict e={e} review={review} />
      <QaChecks episodeId={e.id} />
      {payment && (
        <p className="mt-2 text-xs text-zinc-500">
          Payout {fmtCents(payment.amountCents)}: <StatusPill status={payment.status} /> <span className="text-orange-600 dark:text-orange-400">test mode</span>
        </p>
      )}
    </article>
  );
}

export function EpisodesGrid({ episodes, reviews, payments, people, simRobotIds }: {
  episodes: readonly Episode[]; reviews: readonly Review[]; payments: readonly Payment[]; people: Map<bigint, Contributor>;
  /** TDM-107: each episode is labelled SIM by the robot it was recorded on. */
  simRobotIds?: Set<bigint>;
}) {
  const [filter, setFilter] = useState<FilterKey>('collected');
  const [showAll, setShowAll] = useState(false);
  // Trial chips and cluster examples link to #episode-<id>: reveal that card even if filtered out.
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash.slice(1);
      if (!h.startsWith('episode-')) return;
      setFilter('all');
      setShowAll(true);
      setTarget(h);
    };
    onHash();
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  useEffect(() => {
    if (!target) return;
    const el = document.getElementById(target);
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.classList.add('ring-2', 'ring-sky-500');
      window.setTimeout(() => el.classList.remove('ring-2', 'ring-sky-500'), 2500);
      setTarget(null);
    }
  }, [target, filter, showAll, episodes]);
  const latestReview = new Map<bigint, Review>();
  for (const r of byNewest(reviews).reverse()) latestReview.set(r.episodeId, r);
  const payByEp = new Map(payments.filter((p) => p.episodeId !== 0n).map((p) => [p.episodeId, p]));

  if (episodes.length === 0) return <Empty>No episodes yet. Baseline rollouts show up here first, then the data people record.</Empty>;

  const active = FILTERS.find((f) => f.key === filter) ?? FILTERS[2];
  const list = byNewest(episodes).filter(active.test);
  const rejected = episodes.filter((e) => e.reviewStatus === 'rejected').length;
  const accepted = episodes.filter((e) => e.reviewStatus === 'accepted').length;
  const pending = episodes.filter((e) => e.reviewStatus === 'pending').length;
  const cap = 12;
  const shown = showAll ? list : list.slice(0, cap);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div role="tablist" className="inline-flex rounded-md border border-zinc-200 p-0.5 text-sm dark:border-zinc-800">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              role="tab"
              aria-selected={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded px-2.5 py-1 focus-visible:outline-2 focus-visible:outline-sky-600 ${filter === f.key ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : 'text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100'}`}
            >
              {f.label} <span className="tabular-nums opacity-70">{episodes.filter(f.test).length}</span>
            </button>
          ))}
        </div>
        <p className="text-sm tabular-nums text-zinc-500">
          Reviews: {accepted} accepted, {rejected} rejected, {pending} waiting
        </p>
      </div>
      {list.length === 0 ? (
        <Empty>{filter === 'collected' ? 'No collected episodes yet.' : 'No episodes here yet.'}</Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {shown.map((e) => (
            <EpisodeCard key={e.id.toString()} e={e} review={latestReview.get(e.id)} payment={payByEp.get(e.id)} who={e.contributorId ? people.get(e.contributorId) : undefined} sim={!!simRobotIds?.has(e.robotId)} />
          ))}
        </div>
      )}
      {list.length > cap && (
        <button onClick={() => setShowAll((v) => !v)} className="mt-3 text-sm text-blue-600 hover:underline">
          {showAll ? 'Show fewer' : `Show all ${list.length}`}
        </button>
      )}
    </div>
  );
}
