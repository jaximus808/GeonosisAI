# StatusTag

A stamped label with a square rivet that shows where a ticket, episode or agent is.

- Markup: `<span class="gx-tag gx-tag-running"><i class="gx-rivet"></i>Diagnose</span>`.
- The consumer passes `ticket.status`. Map it to the station label and tone with the table in the brand book: running stages use `gx-tag-running`, `shipped` uses `gx-tag-shipped`, `failed` uses `gx-tag-failed`, `cancelled` uses `gx-tag-cancelled`, and everything else keeps the default `steel`.
- `gx-tag-money` (`ochre`) is for the data market: purchased episodes and paid payouts.
- An agent waiting on a SpacetimeDB predicate shows a default tag that says "Sleeping".
- The text always names the state, so the tag still reads without colour.
