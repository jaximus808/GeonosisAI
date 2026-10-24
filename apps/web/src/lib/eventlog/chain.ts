// event_log chain hash, "rdf-evlog-chain-v1" (TDM-77).
//
// The algorithm, which scripts/verify_event_log.py (TDM-15) must reproduce byte for byte.
// The reference implementation is spikes/tdm-77-events/chain_hash_ref.py.
//
//   rows    = every event_log row, ordered by id ascending (no filtering)
//   rec(r)  = UTF-8 of
//             `${id}\n${ts_micros}\n${len(actor)}:${actor}\n${len(type)}:${type}\n${len(payload_json)}:${payload_json}`
//             where id and ts_micros (microseconds since the Unix epoch) are base-10 integers
//             and len() is the UTF-8 byte length of the string that follows it
//   h_0     = 32 zero bytes
//   h_i     = sha256(h_{i-1} || rec(row_i))      (raw 32-byte digest, then the record bytes)
//   output  = count, first id, last id, lowercase hex of h_n (64 zeros when the log is empty)
//
// Changing any byte of any row, removing a row, or reordering rows changes the output.
// The hash proves that two readers saw the same rows. On its own it does not prove the rows
// were never edited: that needs the module to refuse updates and deletes (TDM-15), plus a
// hash recorded earlier to compare against.

import { sha256, toHex } from './sha256';

export const CHAIN_ALGORITHM = 'rdf-evlog-chain-v1';

export type ChainRow = {
  id: bigint;
  tsMicros: bigint;
  actor: string;
  type: string;
  payloadJson: string;
};

export type IdGap = { after: bigint; before: bigint; missing: bigint };

export type ChainSummary = {
  algorithm: string;
  count: number;
  firstId?: bigint;
  lastId?: bigint;
  firstTsMicros?: bigint;
  lastTsMicros?: bigint;
  hash: string;
  /** Holes in the id sequence between the first and last id. */
  gaps: IdGap[];
  /** Pairs where a higher id carries an earlier ts than the row before it. */
  tsInversions: number;
};

const enc = new TextEncoder();

function field(s: string): string {
  return `${enc.encode(s).length}:${s}`;
}

export function canonicalRecord(r: ChainRow): string {
  return `${r.id}\n${r.tsMicros}\n${field(r.actor)}\n${field(r.type)}\n${field(r.payloadJson)}`;
}

function step(prev: Uint8Array, record: string): Uint8Array {
  const body = enc.encode(record);
  const buf = new Uint8Array(32 + body.length);
  buf.set(prev, 0);
  buf.set(body, 32);
  return sha256(buf);
}

const ZERO = new Uint8Array(32);

/**
 * Incremental chain hasher. Keeps the canonical record and running digest per row, so a
 * new row costs one sha256. A changed or missing earlier row re-hashes from that point on.
 */
export class EventLogChain {
  private records: string[] = [];
  private digests: Uint8Array[] = [];

  /** Chain hash after the row at `index` (ascending order) of the last summarize() call. */
  hashAt(index: number): string | undefined {
    const d = this.digests[index];
    return d ? toHex(d) : undefined;
  }

  summarize(rowsAsc: readonly ChainRow[]): ChainSummary {
    const n = rowsAsc.length;
    let i = 0;
    const keep = Math.min(n, this.records.length);
    const recs = new Array<string>(n);
    for (; i < keep; i++) {
      const rec = canonicalRecord(rowsAsc[i]);
      recs[i] = rec;
      if (rec !== this.records[i]) break;
    }
    this.records.length = i;
    this.digests.length = i;
    for (; i < n; i++) {
      const rec = recs[i] ?? canonicalRecord(rowsAsc[i]);
      const prev = i === 0 ? ZERO : this.digests[i - 1];
      this.records.push(rec);
      this.digests.push(step(prev, rec));
    }

    const gaps: IdGap[] = [];
    let tsInversions = 0;
    for (let j = 1; j < n; j++) {
      const a = rowsAsc[j - 1], b = rowsAsc[j];
      if (b.id - a.id > 1n) gaps.push({ after: a.id, before: b.id, missing: b.id - a.id - 1n });
      if (b.tsMicros < a.tsMicros) tsInversions++;
    }

    return {
      algorithm: CHAIN_ALGORITHM,
      count: n,
      firstId: rowsAsc[0]?.id,
      lastId: rowsAsc[n - 1]?.id,
      firstTsMicros: rowsAsc[0]?.tsMicros,
      lastTsMicros: rowsAsc[n - 1]?.tsMicros,
      hash: toHex(n === 0 ? ZERO : this.digests[n - 1]),
      gaps,
      tsInversions,
    };
  }
}

/** One-shot version for scripts and tests. */
export function chainSummary(rowsAsc: readonly ChainRow[]): ChainSummary {
  return new EventLogChain().summarize(rowsAsc);
}
