# design-sync notes (AMGym Mini App)

- The mini app is a Vite app, not a library: there is no dist. The converter bundles the barrel `miniapp/src/components/index.ts` passed via `--entry`; components are enumerated in `componentSrcMap` because no `.d.ts` tree exists.
- Screens (`miniapp/src/screens/*`) are deliberately excluded: they read the store and the API.
- Colors come from Telegram theme params (`--tg-theme-*`) with browser fallbacks in `src/styles.css` (`:root` `--f-*`), so previews render with the fallback palette.
- `LazyCharts.tsx` re-exports LineSeries/BarSeries lazily for the app; the bundle uses `ProgressChart.tsx` directly.
- Render check: playwright 1.62.0 in `.ds-sync/` matches the cached `chromium-1234`.
- `SF Pro Text` in the font stack is the iOS system font (`-apple-system, ..., system-ui`); nothing ships it, so `runtimeFontPrefixes: ["SF Pro"]` silences `[FONT_MISSING]`. Previews render in the viewer's system font, same as the app.
- Overlays (`Sheet`, `ExerciseSheet`) are `position: fixed`; their previews wrap them in a `transform: translateZ(0)` box so the card captures them. `ConfirmHost` renders fine without it.
- Icons are fixed 24px SVGs (`width`/`height` attrs), they do not scale with font-size; the preview shows colors, not sizes.
- `ExerciseSheet` reads the app store; in a plain browser the store seeds demo history, so the preview shows a chart and past sets.

## Known render warns
- none after authoring; the first build's `[RENDER_BLANK]`/`[RENDER_THIN]` lines were floor cards and went away with the authored previews.

## Re-sync risks
- `miniapp/src/components/index.ts` is the bundle entry: a component added to the app but not to this barrel (and to `componentSrcMap`) is silently absent from the sync.
- `dtsPropsFor` is hand-written from the sources: a prop change in a component needs the matching edit here, nothing checks it.
- Previews use the app's CSS classes (`.card`, `.row`, `.tabbar` …); a class rename in `styles.css` breaks the cards without a build error.
- `ExerciseSheet` preview depends on the demo store seed; if the store stops seeding demo data in a plain browser, the card shows only the program plan.
- Toolchain: node 26, playwright 1.62.0 pinned to the cached chromium-1234; a different cache needs the matching playwright release.
