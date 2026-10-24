// What the /events "untouched" seal is allowed to claim (TDM-77).

/**
 * TDM-15 shipped (published to maincloud): no reducer updates or deletes event_log rows, the
 * only insert is logEvent in spacetime/spacetimedb/src/event_log.ts, and append_event needs a
 * service identity. `python3 -I scripts/verify_event_log.py static` re-checks the source.
 */
export const APPEND_ONLY_ENFORCED = true;

/** The command that prints the same count and chain hash from the terminal. */
export const VERIFY_COMMAND = 'python3 -I scripts/verify_event_log.py';
