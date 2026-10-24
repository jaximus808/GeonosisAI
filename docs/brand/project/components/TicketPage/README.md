# TicketPage

The target layout for `/tickets/[id]`: a title block, the line, then plates for before and after, the planner decision, and purchased episodes.

- Order from top to bottom: the header bar, the title block (id, status tag, iteration, task as `display-l`, robot and target metadata, actions on the right), the **Line** in its own plate, then a two-column grid with **Before and after** and **Planner decision**, then **Purchased episodes**.
- The only `melt` on the page should be the AI in the wordmark, the running station, the Eval tag, the target tick and the single primary button.
- The planner decision always comes in three parts: what failed (with counts), what we are buying (one `body-strong` sentence), and what it cost (an `ochre` meter against the budget).
- Data comes from SpacetimeDB subscriptions on `ticket`, `eval_run`, `eval_trial`, `episode` and `payment`. The mock uses example numbers.
- Below 900px the grid stacks to one column and the line scrolls sideways inside its plate.
