# GeonosisAI brand

The brand system lives in [`docs/brand/project/README.md`](brand/project/README.md) (the brand book), with
`tokens.json` (Foundry dark + Dune light), `components/bundle.css` and per-component READMEs and mocks.

In `apps/web` (TDM-109):
- Tokens: `src/app/globals.css` (`bg-ground`, `bg-surface`, `text-ink-muted`, `border-line`, `text-melt`, `text-arc` ...).
  Foundry is the default; Dune applies under `<html data-theme="light">` (header toggle, or `?theme=light`) and for print.
- Fonts: `next/font/google` in `src/app/layout.tsx` -> `font-stencil` (wordmark only), `font-display`, `font-sans`, `font-mono`.
- Components: `src/components/brand/` (`Wordmark`, `Plate`, `Button`, `StatusTag`/`Tag`/`SimTag`, `Line`, `Readout`,
  `TaskCard`, `Meter`) and the `gx-*` classes in `src/components/brand/brand.css`.
- The Tailwind palette is remapped onto brand hues in `globals.css`, so existing `zinc-*`, `emerald-*`, `violet-*`
  classes already render in brand colours. New code should prefer the token utilities.
