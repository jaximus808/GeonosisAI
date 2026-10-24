# Readout

An eval result shown as a big mono percentage, the count and its Wilson 95% interval, a track with the target marked, and one chip per trial.

- The consumer provides successes, trials, the Wilson lower and upper bounds, the target success rate and a label (iteration and checkpoint name).
- Always show the denominator and the interval. "90%" on its own is not allowed.
- On the track, the interval band is `steel`. It turns `arc` (`is-pass`) only when the lower bound clears the target, or when the ticket's ship rule says the run passes. The point is `ink` and the target is a dashed `melt` tick.
- Trial chips are `P` and `F` in `arc` and `slag`. Clicking a chip opens that episode's video.
- Put Before and After readouts side by side in one plate with the same track scale, so the jump is visible at a glance.
