// TDM-114: robot arbitration types and helpers. The implementation lives in the TDM-114 section of
// lib/tdm112/floor.ts (so the floor model stays import-free for the plain-Node test runner); this is
// the stable import path for components (`@/lib/tdm114/arbitration`).
export {
  tdm114Clock,
  tdm114Dur,
  tdm114EvalPhase,
  tdm114EvalWaiters,
  tdm114HoldOn,
  tdm114Holder,
  tdm114IsSim,
  tdm114JobLabel,
  tdm114JobOf,
  tdm114LiveIntervention,
  tdm114Queue,
  tdm114Ref,
  tdm114WaitingFor,
} from '../tdm112/floor';
export type {
  Tdm114Command,
  Tdm114EvalPhase,
  Tdm114EvalRunLite,
  Tdm114Event,
  Tdm114Holder,
  Tdm114HolderState,
  Tdm114Job,
  Tdm114LivePolicy,
  Tdm114QueueEntry,
  Tdm114Robot,
  Tdm114TicketLite,
  Tdm114TrialLite,
  Tdm114WaitReason,
  Tdm114WaitingFor,
} from '../tdm112/floor';
