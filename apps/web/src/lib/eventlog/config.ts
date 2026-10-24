// What the /events "untouched" seal is allowed to claim (TDM-77).

/**
 * Flip to true only once TDM-15 has shipped: no reducer updates or deletes event_log rows,
 * checked by grep over spacetime/spacetimedb/src. Until then the seal says append-only is
 * a convention, not a module rule.
 */
export const APPEND_ONLY_ENFORCED = false;

/** The command that prints the same count and chain hash from the terminal. */
export const VERIFY_COMMAND = 'python3 -I spikes/tdm-77-events/chain_hash_ref.py';
