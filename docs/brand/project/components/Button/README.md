# Button

Square-cornered buttons. The secondary outline style is the default, and a view gets at most one `melt` primary.

- `gx-btn`: the default. A `line-strong` outline in `ink`.
- `gx-btn-primary`: `melt` fill with `on-melt` text. Use it once per view, for the step that moves the ticket down the line ("Start baseline", "Ship checkpoint", "Claim task").
- `gx-btn-danger`: a `slag` outline. Only for the soft e-stop and for destructive actions.
- `gx-btn-ghost`: no border, `ink-muted` text. For cancel and dismiss.
- `gx-btn-block`: full width and 44px tall. Use it for the action at the bottom of a phone task card.
- Labels are verb first, in sentence case, and at most three words.
- Disabled buttons drop to 40% opacity and always come with a caption that says what they are waiting for.
- Focus shows a 2px `focus-ring` outline, offset 2px.
