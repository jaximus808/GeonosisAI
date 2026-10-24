# PhoneTask

The target layout for the contributor's phone pages (`/marketplace`, `/join`, `/earnings`): a compact header, today's earnings, and a single column of **TaskCard**s.

- The header has `gx-wordmark-sm` on the left and a live dot on the right for the robot the tasks belong to.
- Earnings are shown in `ochre` mono and are the only big number on the screen.
- The first card is the one the planner needs most, and it is the only one with a `melt` primary button. The cards below it use the default button.
- The page ends with "Test mode. No real money moves." while Stripe is in test mode.
- The teleop and reset pages keep this header and replace the cards with the camera wells, a full-width `gx-btn-danger` "Soft e-stop" fixed to the bottom, and a step count ("Takeover · 0:42").
