# AMGym Mini App — how to build with it

Telegram Mini App for a personal gym log (workouts, programs, nutrition). Phone width only (360–430 px), Russian UI copy.

## Setup
No provider. Mount components anywhere; load `styles.css` once. Everything is themed through CSS custom properties that follow the Telegram theme (`--tg-theme-*`) with built-in fallbacks for a plain browser, so nothing needs to be passed in. Overlays (`Sheet`, `ExerciseSheet`, `ConfirmHost`) are `position: fixed` and cover the viewport; `ConfirmHost` is mounted once and opened with `askInPage('Удалить?')`, which resolves to `true`/`false`.

## Styling idiom: tokens + the app's class vocabulary
Never hardcode colors. Use the tokens from `styles.css`:
- surfaces: `var(--bg)` page, `var(--card)` cards, `var(--soft)` subtle fills, `var(--sep)` separators
- text: `var(--text)`, `var(--hint)` secondary, `var(--section-title)` eyebrow headings, `var(--link)`
- action: `var(--accent)` with `var(--accent-text)`, `var(--danger)` destructive, `var(--done)` success
- intensity: `var(--heavy)`, `var(--medium)`; shape: `var(--radius)` (cards/buttons), `var(--tabbar-h)`

Layout and controls are plain elements with these classes (read `_ds_bundle.css` for the exact rules):
- containers: `.app`, `.stack`, `.card`, `.chart-card`, `.list` with `.row` children (`.grow` fills the row), `.tiles` + `.tile` stat grid, `.hero` + `.hero-meta`
- typography: `h1` 28px, `h2` eyebrow uppercase, `.hint` / `.muted` secondary text, `.num` tabular numbers, `.eyebrow`
- controls: `.btn` (primary; add `.secondary`), `.icon-btn`, `.field` text input (`.invalid` state), `.segmented`, `.chips` + `.chip`, `.select`
- domain pieces: `.w-sets` + `.w-set` set chips, `.badge` (`.heavy`, `.medium`, `.drop`), `.set-row`, `.date-nav`
- navigation: `.tabbar` + `.tab` (`.active`) at the bottom, icons `IconToday`, `IconProgram`, `IconHistory`, `IconProgress`, `IconNutrition`

## Where the truth lives
`styles.css` → `_ds_bundle.css` (all tokens and classes), `components/<group>/<Name>/<Name>.prompt.md` and `.d.ts` per component.

## Idiomatic snippet
```tsx
<div className="card">
  <div className="row">
    <div className="grow">Жим лёжа</div>
    <IntensityBadge value="heavy" />
  </div>
  <div className="w-sets"><span className="w-set">80 × 8</span><span className="w-set">80 × 7</span></div>
  <div className="row" style={{ gap: 12 }}>
    <div className="grow">Вес, кг</div>
    <div style={{ width: 96 }}><NumField value={80} onChange={() => {}} decimal /></div>
  </div>
  <button className="btn">Сохранить</button>
</div>
```
