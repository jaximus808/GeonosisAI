GeonosisAI is the droid foundry for real robots. You plug in a robot and file a ticket ("put the cup in the bin, 80% reliable, $50 budget"). The foundry runs a baseline, works out why the robot fails, buys exactly the data that fixes those failures from people on their phones, fine-tunes the policy, evaluates it and ships it. **Ticket in. Deployed robot out.**

The name is a nod to the droid foundry on Geonosis in *Attack of the Clones*. We borrow the setting, not the franchise: molten metal, red rock, welding arcs, stamped steel and an assembly line that never stops. Every surface should feel like a control room overlooking that floor.

## The name

- Write it **GeonosisAI**: one word, capital G, capital AI. Never "Geonosis AI", "Geonosis.ai" or "GEONOSIS-AI" in running text.
- The wordmark is the one place the name is set in uppercase stencil. Use the **Wordmark** component; never retype it in another face.
- In a sentence the product is "GeonosisAI" or "the foundry". A ticket's journey is "the line".

## Voice

Factory-floor plain. The foundry reports what it did and what it will do next, in numbers.

- Short sentences. Verbs first on controls: "Start baseline", "Ship checkpoint", "Claim task".
- Numbers over adjectives. Write "9/10 at eval, up from 3/10", never "great improvement".
- Always show the denominator and the interval with a success rate: "90% (60–98%, n=10)".
- Money is dollars with cents: "$3.50", "$31.50 of $50.00". Say "test mode" wherever money appears in a demo.
- Explain decisions the way a foreman would: what failed, what we are buying, why. Real example: "6 of 7 failures happen in the last 5 cm. Buy recovery takeovers, not clean demos."
- Sentence case everywhere except the wordmark and station labels.
- No emoji, no exclamation marks, no "magic", no "revolutionary".
- People who teleoperate are **contributors**. Robots are named by id (`yam-01`). Agents are **Factory Droids**.

### The words of the line

Use these names for ticket stages in UI, in the same order. They map one to one onto `ticket.status`.

| Station label | `ticket.status` | Tag tone |
|---|---|---|
| Queued | `created`, `onboarding` | `steel` |
| Baseline | `baseline` | `melt` while running |
| Diagnose | `diagnose` | `melt` while running |
| Plan data | `plan_data` | `melt` while running |
| Collect | `dispatch` | `melt` while running |
| Review | `review` | `melt` while running |
| Train | `train` | `melt` while running |
| Eval | `eval` | `melt` while running |
| Shipped | `shipped` | `arc` |
| Failed | `failed` | `slag` |
| Cancelled | `cancelled` | `steel` |

## Don't borrow the franchise

GeonosisAI is a hackathon project, not a licensed product.

- Never use Star Wars logos, the Star Wars or Star Jedi typefaces, Aurebesh, droid or character likenesses, film stills, quotes or sound effects.
- No lightsaber glows, starfields or yellow crawl text.
- One spoken line in the pitch is enough: "It's the droid foundry from Geonosis, but for real robots."

## Color

Dark first. **Foundry** is the primary theme and the one the demo runs in. **Dune** is the light theme for printed reports and daylight. Both share every token name.

- Pages sit on `ground`. Panels are **plates**: `surface` with a 1px `line` border and `radius-sm`.
- `melt` is the brand. Spend it on three things only: the AI in the wordmark, the one primary button per view, and the station that is running right now. If more than one thing on a screen is orange, one of them is wrong.
- `ochre` means money and the data market: budget meters, prices, payouts.
- `arc` means good and live: passed trials, the live connection dot, Shipped, links and the focus ring.
- `slag` means failure or danger: failed trials, Failed, Soft e-stop. Always pair it with a word.
- `steel` means idle or waiting: queued tickets, sleeping agents, cancelled work.
- Text on a coloured fill uses the matching `on-` token (`on-melt`, `on-arc`). Never hard-code white.
- Pass and fail are told apart by blue against red and by the words "Pass" and "Fail", so they survive colour blindness.

## Type

- `wordmark` (Big Shoulders Stencil) for the name only.
- `display-xl`, `display-l` and `heading` (Big Shoulders) for titles. Use `display-xl` once per screen at most.
- `station` (Big Shoulders, uppercase, tracked) for station labels and status tags.
- `body`, `body-strong`, `small` and `label` (IBM Plex Sans) for everything people read.
- `readout`, `data` and `id` (IBM Plex Mono, tabular figures) for every number a robot or ledger produced: success rates, money, step counts, ids.
- Load all three families from Google Fonts. In the Next.js app use `next/font/google` (`Big_Shoulders`, `Big_Shoulders_Stencil`, `IBM_Plex_Sans`, `IBM_Plex_Mono`) and expose them as `--font-stencil`, `--font-display`, `--font-sans` and `--font-mono`.

## Shape and layout

- **Plates, not cards.** Square corners (`radius-sm`), 1px `line` borders, no shadows. `shadow-overlay` is only for things that float: menus, dialogs, toasts.
- **The line** is the signature element. Every ticket view puts its stations across the top as rivets on a track (the **Line** component). Done stations are filled `ink`, the running station is `melt`, upcoming stations are hollow `steel`.
- **Rivets.** A 6px square dot is the brand's bullet. Use it in tags, legends and the line. Never use round dots.
- Phone pages (task, teleop, reset, join) use one column with a `space-4` gutter, `radius-lg` task cards and a full-width primary button at the bottom of each card.
- Desktop pages use a `space-8` gutter, a 1120px max width and plates in a two-column grid with a `space-6` gap.
- Spacing comes from the 4px scale (`space-1` to `space-12`). Never use one-off values.

## Imagery and motion

- Real camera frames from the robot are the only photography. Show them in `surface-sunken` wells with `radius-md`, labelled with `id` text (`exo`, `wrist-l`, `wrist-r`).
- No stock photos, renders of humanoids, glowing brains or circuit-board backgrounds.
- Motion is mechanical. Use 120–200 ms ease-out for state changes. The running station's rivet may pulse once every 2 s. Turn all of it off under `prefers-reduced-motion`.

## Iconography

No icon set ships with the system. Prefer words. Where an icon is unavoidable (e-stop, camera, play), use Lucide at 16px with a 1.5px stroke in `currentColor`, sitting next to a text label.

## States

- Focus: a 2px solid `focus-ring` outline, offset 2px, on every interactive element.
- Hover on a plate row: the background moves to `surface-sunken`.
- Disabled controls: 40% opacity and a reason in a tooltip or caption ("Waiting for 8 accepted episodes").
- Empty states name what will appear and how to start: "No tickets yet. File one to put a robot on the line."

## Applying it to apps/web

The web app is Next.js with Tailwind v4 and currently uses zinc defaults. To move it onto this system:

1. Rename the product to GeonosisAI in `src/app/layout.tsx` metadata, `manifest.ts` and the `Header`.
2. Declare the tokens in `globals.css` under `@theme` as `--color-<token>` (for example `--color-melt`, `--color-surface-sunken`), with Dune values under `:root` and Foundry values under `@media (prefers-color-scheme: dark)`. Then `bg-ground`, `text-ink-muted` and `border-line` work as utilities.
3. Replace `bg-zinc-*` and `text-zinc-*` with the token utilities. Replace ad-hoc status colours with the tag tones in the table above.
4. Port the classes in `components/bundle.css` (`gx-plate`, `gx-tag`, `gx-line`, `gx-readout`, `gx-btn`, `gx-task`) into React components under `src/components/brand/`.
5. Build the ticket page and the phone task page to match the **Ticket page** and **Phone task** mocks.
