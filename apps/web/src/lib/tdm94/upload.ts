'use client';

// TDM-94: the seller's upload path, all in the browser.
//
//   1. pick or drop files / folders -> planDelivery() groups them into episodes
//        raw:     <episode folder>/<camera>.mp4 ... + actions.parquet   (TDM-25 episode format)
//        lerobot: a LeRobot v2.1 folder (meta/info.json, meta/episodes.jsonl, data/, videos/)
//   2. POST /api/tdm94/offers/{id}/uploads/sign  -> signed PUT URLs, only for open slots
//   3. PUT each file straight to its signed URL (XHR, for progress); bytes never pass our server
//   4. POST /api/tdm94/offers/{id}/episodes      -> the manifest; extras come back refused
//
// frame_count comes from the actions parquet footer (num_rows) and duration from its rdf.episode fps,
// read here with a small Thrift reader so no parquet library is needed.

import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';

export type PickedFile = { path: string; file: File }; // path relative to what was dropped, "/"-separated

const REF_OK = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function safeRef(name: string): string {
  const s = name.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[^A-Za-z0-9]+/, '').slice(0, 128);
  return REF_OK.test(s) ? s : `episode-${Date.now()}`;
}

// ---------------------------------------------------------------- picking

type Entry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (ok: (f: File) => void, err: (e: unknown) => void) => void;
  createReader?: () => { readEntries: (ok: (es: Entry[]) => void, err: (e: unknown) => void) => void };
};

async function walk(entry: Entry, prefix: string, out: PickedFile[]): Promise<void> {
  if (entry.isFile && entry.file) {
    const file = await new Promise<File>((ok, err) => entry.file!(ok, err));
    out.push({ path: prefix + entry.name, file });
  } else if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise<Entry[]>((ok, err) => reader.readEntries(ok, err));
      if (batch.length === 0) break;
      for (const e of batch) await walk(e, `${prefix}${entry.name}/`, out);
    }
  }
}

/** Files and whole folders from a drop event, with their relative paths. */
export async function filesFromDrop(dt: DataTransfer): Promise<PickedFile[]> {
  const out: PickedFile[] = [];
  const entries: Entry[] = [];
  for (const item of Array.from(dt.items)) {
    const e = (item as DataTransferItem & { webkitGetAsEntry?: () => Entry | null }).webkitGetAsEntry?.();
    if (e) entries.push(e);
  }
  if (entries.length === 0) return Array.from(dt.files).map((file) => ({ path: file.webkitRelativePath || file.name, file }));
  for (const e of entries) await walk(e, '', out);
  return out;
}

/** Files from an <input type=file> (folder pickers fill webkitRelativePath). */
export function filesFromInput(list: FileList | null): PickedFile[] {
  return Array.from(list ?? []).map((file) => ({ path: file.webkitRelativePath || file.name, file }));
}

// ---------------------------------------------------------------- grouping

export type RawEpisodePlan = {
  ref: string;
  folder: string;
  cameras: PickedFile[]; // <camera>.mp4
  actions: PickedFile | undefined;
  problems: string[];
};

export type DeliveryPlan =
  | { format: 'raw'; episodes: RawEpisodePlan[]; ignored: string[] }
  | { format: 'lerobot'; root: string; files: { rel: string; file: File }[]; episodeIndexes: number[]; problems: string[] };

const isHidden = (p: string) => p.split('/').some((s) => s.startsWith('.') || s === '__MACOSX');
const dirOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
const baseOf = (p: string) => p.slice(p.lastIndexOf('/') + 1);

export function planDelivery(picked: PickedFile[]): DeliveryPlan {
  const files = picked.filter((f) => !isHidden(f.path));
  const info = files.find((f) => f.path === 'meta/info.json' || f.path.endsWith('/meta/info.json'));
  if (info) {
    const root = info.path.slice(0, info.path.length - 'meta/info.json'.length);
    const inside = files.filter((f) => f.path.startsWith(root)).map((f) => ({ rel: f.path.slice(root.length), file: f.file }));
    const idx = new Set<number>();
    for (const f of inside) {
      const m = /episode_(\d+)\.(?:parquet|mp4)$/.exec(f.rel);
      if (m) idx.add(Number(m[1]));
    }
    const problems: string[] = [];
    if (!inside.some((f) => f.rel === 'meta/episodes.jsonl')) problems.push('meta/episodes.jsonl is missing');
    if (idx.size === 0) problems.push('no data/…/episode_*.parquet files found');
    return { format: 'lerobot', root: root.replace(/\/$/, '') || '(dropped folder)', files: inside, episodeIndexes: [...idx].sort((a, b) => a - b), problems };
  }

  const byDir = new Map<string, PickedFile[]>();
  for (const f of files) {
    const d = dirOf(f.path);
    byDir.set(d, [...(byDir.get(d) ?? []), f]);
  }
  const episodes: RawEpisodePlan[] = [];
  const ignored: string[] = [];
  for (const [folder, list] of [...byDir.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const cameras = list.filter((f) => /\.mp4$/i.test(f.path));
    const actions = list.find((f) => baseOf(f.path) === 'actions.parquet');
    const other = list.filter((f) => f !== actions && !cameras.includes(f));
    if (!cameras.length && !actions) {
      ignored.push(...other.map((f) => f.path));
      continue;
    }
    ignored.push(...other.map((f) => f.path));
    const problems: string[] = [];
    if (!actions) problems.push('no actions.parquet in this folder');
    if (!cameras.length) problems.push('no camera .mp4 in this folder');
    const name = folder ? baseOf(folder) : 'episode-1';
    episodes.push({ ref: safeRef(name), folder: folder || '(loose files)', cameras, actions, problems });
  }
  return { format: 'raw', episodes, ignored };
}

export const cameraName = (f: PickedFile) => baseOf(f.path).replace(/\.mp4$/i, '');

// ---------------------------------------------------------------- parquet footer (Thrift compact)

class Compact {
  pos = 0;
  constructor(private b: Uint8Array) {}
  byte() {
    if (this.pos >= this.b.length) throw new Error('parquet footer ended early');
    return this.b[this.pos++];
  }
  varint(): bigint {
    let shift = 0n;
    let out = 0n;
    for (;;) {
      const x = this.byte();
      out |= BigInt(x & 0x7f) << shift;
      if (!(x & 0x80)) return out;
      shift += 7n;
    }
  }
  zigzag(): bigint {
    const v = this.varint();
    return (v >> 1n) ^ -(v & 1n);
  }
  bytes(): Uint8Array {
    const n = Number(this.varint());
    const s = this.b.subarray(this.pos, this.pos + n);
    this.pos += n;
    return s;
  }
  skip(type: number): void {
    switch (type) {
      case 1:
      case 2:
        return; // bool in a field header
      case 3:
        this.pos += 1;
        return;
      case 4:
      case 5:
      case 6:
        this.varint();
        return;
      case 7:
        this.pos += 8;
        return;
      case 8:
        this.bytes();
        return;
      case 9:
      case 10: {
        const { size, elem } = this.listHeader();
        for (let i = 0; i < size; i++) {
          if (elem === 1 || elem === 2) this.pos += 1;
          else this.skip(elem);
        }
        return;
      }
      case 11: {
        const size = Number(this.varint());
        if (size === 0) return;
        const kv = this.byte();
        for (let i = 0; i < size; i++) {
          this.skip(kv >> 4);
          this.skip(kv & 0x0f);
        }
        return;
      }
      case 12:
        this.struct(() => false);
        return;
      default:
        throw new Error(`unknown thrift type ${type}`);
    }
  }
  listHeader() {
    const h = this.byte();
    let size = h >> 4;
    if (size === 15) size = Number(this.varint());
    return { size, elem: h & 0x0f };
  }
  /** Reads a struct; onField returns true when it consumed the field itself. */
  struct(onField: (id: number, type: number) => boolean): void {
    let last = 0;
    for (;;) {
      const h = this.byte();
      if (h === 0) return;
      const type = h & 0x0f;
      const delta = h >> 4;
      const id = delta ? last + delta : Number(this.zigzag());
      last = id;
      if (!onField(id, type)) this.skip(type);
    }
  }
}

export type ParquetFooter = { numRows: number; kv: Record<string, string> };

export async function readParquetFooter(file: File): Promise<ParquetFooter> {
  if (file.size < 12) throw new Error('not a parquet file (too small)');
  const tail = new Uint8Array(await file.slice(file.size - 8).arrayBuffer());
  if (String.fromCharCode(...tail.subarray(4)) !== 'PAR1') throw new Error('not a parquet file (no PAR1 footer)');
  const len = new DataView(tail.buffer).getUint32(0, true);
  if (len <= 0 || len > file.size - 8) throw new Error('parquet footer length is invalid');
  const meta = new Uint8Array(await file.slice(file.size - 8 - len, file.size - 8).arrayBuffer());
  const r = new Compact(meta);
  const dec = new TextDecoder();
  let numRows = -1;
  const kv: Record<string, string> = {};
  r.struct((id, type) => {
    if (id === 3 && type === 6) {
      numRows = Number(r.zigzag());
      return true;
    }
    if (id === 5 && type === 9) {
      const { size } = r.listHeader();
      for (let i = 0; i < size; i++) {
        let k = '';
        let v = '';
        r.struct((fid, ft) => {
          if (fid === 1 && ft === 8) k = dec.decode(r.bytes());
          else if (fid === 2 && ft === 8) v = dec.decode(r.bytes());
          else return false;
          return true;
        });
        kv[k] = v;
      }
      return true;
    }
    return false;
  });
  if (numRows < 0) throw new Error('parquet footer has no row count');
  return { numRows, kv };
}

function videoSeconds(file: File): Promise<number> {
  return new Promise((ok, err) => {
    const v = document.createElement('video');
    const url = URL.createObjectURL(file);
    v.preload = 'metadata';
    v.onloadedmetadata = () => {
      URL.revokeObjectURL(url);
      Number.isFinite(v.duration) && v.duration > 0 ? ok(v.duration) : err(new Error('no duration'));
    };
    v.onerror = () => {
      URL.revokeObjectURL(url);
      err(new Error('the browser could not read this video'));
    };
    v.src = url;
  });
}

export type Timing = { frameCount: number; durationMs: number; source: string; fixture: boolean };

/** frame_count = parquet rows; duration from rdf.episode fps, else from the first camera's length. */
export async function episodeTiming(ep: RawEpisodePlan): Promise<Timing> {
  if (!ep.actions) throw new Error('no actions.parquet');
  const footer = await readParquetFooter(ep.actions.file);
  let fps = 0;
  let fixture = false;
  try {
    const meta = JSON.parse(footer.kv['rdf.episode'] ?? '{}');
    fps = Number(meta.fps) || 0;
    fixture = meta.fixture === true;
  } catch {
    // not our metadata; fall back to the video
  }
  if (footer.numRows <= 0) throw new Error('actions.parquet has no rows');
  if (fps > 0) return { frameCount: footer.numRows, durationMs: Math.round((footer.numRows / fps) * 1000), source: `${footer.numRows} ticks at ${fps} fps`, fixture };
  const secs = ep.cameras[0] ? await videoSeconds(ep.cameras[0].file) : 0;
  if (secs > 0) return { frameCount: footer.numRows, durationMs: Math.round(secs * 1000), source: `${footer.numRows} ticks, ${secs.toFixed(1)} s of video`, fixture };
  throw new Error('could not tell the episode length (no fps in the parquet, unreadable video)');
}

// ---------------------------------------------------------------- transfer

/** PUT one file to a signed URL with upload progress (bytes go straight to storage). */
export function putSigned(url: string, headers: Record<string, string>, file: File, onProgress: (sent: number) => void): Promise<void> {
  return new Promise((ok, err) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    for (const [k, v] of Object.entries(headers ?? {})) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? (onProgress(file.size), ok()) : err(new Error(`storage answered ${xhr.status}: ${xhr.responseText.slice(0, 200)}`)));
    xhr.onerror = () => err(new Error('upload failed: storage not reachable (network or CORS)'));
    xhr.send(file);
  });
}

export class OfferApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body: unknown,
  ) {
    super(message);
  }
}

function browserToken(): string {
  try {
    return window.localStorage.getItem(BROWSER_TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

/** The server's own message out of a FastAPI error body ({detail: {code, message}} or {detail: str}). */
export function serverMessage(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown })?.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const m = (d as { message?: unknown }).message;
    if (typeof m === 'string') return m;
  }
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string }).msg ?? JSON.stringify(x)).join('; ');
  return fallback;
}

/**
 * POST through the Next proxy (/api/tdm94). It sends this browser's own SpacetimeDB identity token
 * (never the owner token) so the proxy can check the caller is the offer's seller.
 */
export async function offerPost<T = unknown>(offerId: bigint, action: string, body: unknown = {}): Promise<T> {
  const res = await fetch(`/api/tdm94/offers/${offerId}/${action}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${browserToken()}` },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed: unknown = text;
  try {
    parsed = JSON.parse(text);
  } catch {
    // keep text
  }
  if (!res.ok) throw new OfferApiError(res.status, serverMessage(parsed, `HTTP ${res.status}`), parsed);
  return parsed as T;
}

// Shapes from offers_api.py
export type SignedFile = { filename?: string; file?: string; upload_url: string; method: string; headers: Record<string, string>; storage_uri: string; key: string };
export type SignResponse = {
  offer_id: number;
  open_slots: number;
  uploads: ({ ref: string; files: SignedFile[] } | SignedFile)[];
  refused: { ref?: string; file?: string; reason: string }[];
};
export type IngestResponse = {
  ingested: { ref: string; episode_id: number }[];
  already_ingested: { ref: string; episode_id: number }[];
  refused: { ref: string; code: string; reason: string }[];
  open_slots: number;
  needed_count: number;
};
