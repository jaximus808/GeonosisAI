'use client';

import { useSpacetimeDB, useTable } from 'spacetimedb/react';
import type { RowTypedQuery } from 'spacetimedb';
import { tables } from '@/module_bindings';

export type TableName = keyof typeof tables;
type TableRef<K extends TableName> = (typeof tables)[K];
type WhereFn<K extends TableName> = Parameters<TableRef<K>['where']>[0];
/** Row type of a table, e.g. RowOf<'ticket'>. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type RowOf<K extends TableName> = TableRef<K> extends RowTypedQuery<infer Row, any> ? Row : never;

/**
 * Live rows of one table, optionally filtered server-side.
 *   const [events, ready] = useRows('eventLog');
 *   const [eps] = useRows('episode', (r) => r.ticketId.eq(id));
 * Names are the camelCase accessors from the generated bindings.
 */
export function useRows<K extends TableName>(name: K, where?: WhereFn<K>): readonly [readonly RowOf<K>[], boolean] {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const ref = tables[name] as any;
  const query = where ? ref.where(where) : ref;
  return useTable(query) as unknown as readonly [readonly RowOf<K>[], boolean];
}

export type ConnectionStatus = 'connected' | 'connecting' | 'disconnected';

export function useConnectionStatus() {
  const state = useSpacetimeDB();
  const status: ConnectionStatus = state.isActive
    ? 'connected'
    : state.connectionError
      ? 'disconnected'
      : 'connecting';
  return {
    status,
    identity: state.identity?.toHexString(),
    error: state.connectionError?.message,
  };
}

/** True when the generated bindings include a table (lets pages light up after gen:bindings). */
export function hasTable(name: string): name is TableName {
  return Object.prototype.hasOwnProperty.call(tables, name);
}

/** One ticket by id, live. Returns [ticket | undefined, ready]. */
export function useTicket(id: bigint) {
  const [rows, ready] = useRows('ticket', (r) => r.id.eq(id));
  return [rows[0], ready] as const;
}

/** One robot by id, live. Returns [robot | undefined, ready]. */
export function useRobot(id: bigint) {
  const [rows, ready] = useRows('robot', (r) => r.id.eq(id));
  return [rows[0], ready] as const;
}
