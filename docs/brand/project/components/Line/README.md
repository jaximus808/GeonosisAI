# Line

The brand's signature element: a ticket's stations shown as square rivets on a track, from Baseline to Ship.

- The consumer provides the stations in order, each with a state (`is-done`, `is-current` or neither) and a one-line note made of real numbers ("3/10", "8 takeovers", "4,000 steps").
- Set `--stations` on the `ol` to the station count. Mark the running station with `aria-current="step"`.
- Put it across the top of every ticket view, under the title and above the plates.
- When an eval sends the ticket back (`eval` to `diagnose`, `plan_data` or `dispatch`), reset the stations after the loop-back point to upcoming and put "Iteration 2 of 3" next to the title. Don't draw a second line.
- On a failed ticket, the station where it stopped takes `slag` and the note gives the reason.
- On phones, show the running station's tag and an "Eval · 7 of 8" step count instead of the full line.
- The running rivet pulses every 2 s, except under `prefers-reduced-motion`.
