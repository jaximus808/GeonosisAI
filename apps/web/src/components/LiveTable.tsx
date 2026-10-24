'use client';

import { useTable } from 'spacetimedb/react';
import { tables } from '@/module_bindings';
import { hasTable } from '@/lib/spacetime/hooks';

function cell(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'bigint') return v.toString();
  if (typeof v === 'object' && v && 'toDate' in v && typeof (v as { toDate: unknown }).toDate === 'function') {
    return (v as { toDate: () => Date }).toDate().toLocaleString();
  }
  if (typeof v === 'object' && v && 'toHexString' in v) return String((v as { toHexString: () => string }).toHexString()).slice(0, 12) + '…';
  if (typeof v === 'object') return JSON.stringify(v, (_k, x) => (typeof x === 'bigint' ? x.toString() : x));
  return String(v);
}

function Rows({ name }: { name: string }) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [rows, ready] = useTable((tables as any)[name]);
  if (!ready) return <p className="text-sm text-zinc-500">Subscribing…</p>;
  if (rows.length === 0) return <p className="text-sm text-zinc-500">No rows yet.</p>;
  const cols = Object.keys(rows[0] as object);
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-900">
          <tr>{cols.map((c) => <th key={c} className="px-3 py-2">{c}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c} className="max-w-xs truncate px-3 py-2 text-xs">{cell((r as Record<string, unknown>)[c])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Generic live view of any table by its camelCase accessor. Renders a notice when the
 * table is not in the generated bindings yet; rerun `pnpm --filter @rdf/web gen:bindings`
 * after the schema is published and it lights up with no code change.
 */
export function LiveTable({ name }: { name: string }) {
  if (!hasTable(name)) {
    return (
      <p className="rounded-lg border border-dashed border-zinc-300 p-4 text-sm text-zinc-500 dark:border-zinc-700">
        Table <code className="font-mono">{name}</code> is not in the generated bindings yet. Publish the schema, then run{' '}
        <code className="font-mono">pnpm --filter @rdf/web gen:bindings</code>.
      </p>
    );
  }
  return <Rows name={name} />;
}
