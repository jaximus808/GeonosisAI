// TDM-58: recording messages on the teleop /control socket (docs/WORKER_GUIDE.md, "Teleop contract").
//
// Robot side: services/robot-client/src/rdf_robot_client/tdm58_recording.py (RecordingControl).
// The /control WebSocket itself belongs to the teleop page (TDM-56) and the robot executor (TDM-52);
// <RecordingControls/> only needs something that can send a JSON message and hand it the robot's
// messages, i.e. a `ControlChannel`. TDM-56's socket client satisfies it with two methods.
//
// browser -> robot  {t:"record_start", req}            {t:"record_stop", success:true, note?, req}
//                   {t:"record_discard", reason?, req} {t:"takeover", req}   {t:"handback", req}
// robot -> browser  {t:"record_ack", op, req, ok, error?, recording}   and   {t:"state", ..., recording}

export type RecordingPhase = 'idle' | 'recording' | 'policy' | 'human' | 'handed_back';
export type RecordingKind = 'clean_teleop' | 'recovery_takeover';

export interface UploadStatus {
  state: 'none' | 'pending' | 'retrying' | 'done' | 'failed';
  files_done: number;
  files_total: number;
  attempts?: number;
  error?: string | null;
  episode_id?: number | null;
  storage_uri?: string | null;
  next_retry_in_s?: number | null;
}

export interface LastRecording {
  key: string | null;
  kind: string | null;
  status: 'recorded' | 'failed' | 'empty' | 'discarded';
  problems: string[];
  frame_count: number;
  duration_ms: number;
  success: boolean | null;
  takeover_ts: number | null; // unix micros, robot clock
  handback_ts: number | null;
  stopped_at_us: number | null;
  fixture?: boolean; // sim arm / synthetic cameras: not real robot data
  upload: UploadStatus;
}

export interface RecordingState {
  v: 1;
  active: boolean;
  phase: RecordingPhase;
  kind: string | null;
  key: string | null;
  /** 'rollout': a paused rollout's own recording (TDM-69); only takeover/handback apply, the rollout ends it. */
  owner?: 'operator' | 'rollout' | null;
  ticket_id: number | null;
  task_id: number | null;
  started_at_us: number | null;
  elapsed_ms: number;
  frames: number;
  takeover_ts: number | null;
  handback_ts: number | null;
  takeover_at_ms: number | null;
  handback_at_ms: number | null;
  can: { start: boolean; takeover: boolean; handback: boolean; stop: boolean; discard: boolean };
  last: LastRecording | null;
}

export type RecordingOp = 'record_start' | 'record_stop' | 'record_discard' | 'takeover' | 'handback';

export interface RecordAck {
  t: 'record_ack';
  op: RecordingOp;
  req: string | number | null;
  ok: boolean;
  error?: string;
  recording: RecordingState;
}

/** Any robot -> browser message on /control (only `t`, and `recording` where present, are read here). */
export interface RobotControlMessage {
  t: string;
  recording?: RecordingState;
  [k: string]: unknown;
}

/** What <RecordingControls/> needs from the /control socket. */
export interface ControlChannel {
  /** Send one JSON message. Return false when the socket is not open (the button shows an error). */
  send(msg: Record<string, unknown>): boolean;
  /** Robot -> browser messages, parsed. Returns an unsubscribe function. */
  subscribe(fn: (msg: RobotControlMessage) => void): () => void;
}

/** Reviewer limits the UI warns about (services/orchestrator/config/review.yaml). */
export const REVIEW_MIN_S = 2;
export const REVIEW_MAX_S = 120;

export function fmtClock(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${(s - m * 60).toFixed(1).padStart(4, '0')}`;
}

/** Wall-clock time of a robot timestamp (unix micros), with milliseconds. */
export function fmtRobotTs(us: number | null | undefined): string {
  if (us == null) return '';
  const d = new Date(us / 1000);
  return `${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

let reqSeq = 0;
export function nextReq(): string {
  reqSeq += 1;
  return `tdm58-${Date.now().toString(36)}-${reqSeq}`;
}
