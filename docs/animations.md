# Animation conventions

The dashboard has a "Reduced motion" setting (Settings → General, `reduced_motion`
in `SETTING_DEFAULTS`) and also respects the OS-level `prefers-reduced-motion`.
Both feed a single JS flag, `MC_RM` (`static/dashboard.html`), and a matching DOM
attribute, `html[data-reduced-motion="1"]`.

## Which path does your animation take?

New animations must use one of these two mechanisms — never invent a third.

- **Pure CSS** (`transition`, `@keyframes`) → §1, nothing to do.
- **JS stepping a value** (e.g. via `requestAnimationFrame`) → §2, route through `mcCountUp()`.

## 1. CSS transitions / `@keyframes` → nothing to do

A global kill switch near the top of the `<style>` block silences every CSS
transition/animation in the app:

```css
html[data-reduced-motion="1"] *,
html[data-reduced-motion="1"] *::before,
html[data-reduced-motion="1"] *::after{transition:none!important;animation:none!important}
```

Write ordinary CSS transitions/animations, no `MC_RM` check needed. A gauge arc,
a progress bar, a pulsing badge — all covered the moment they ship, present and
future, with zero per-element opt-out.

## 2. JS-driven value tweens → route through `mcCountUp()`

`mcCountUp(el, target, duration, decimals)` (`static/dashboard.html`) steps
`textContent` over a series of `requestAnimationFrame` calls. It's the one
JS-driven tween in the app today, used for every gauge percentage, the
burn-rate figures, and the cost cards. It already checks `MC_RM` (and
`LIVE_TICK`, so a number doesn't re-animate on every ~2s live refresh — a
separate concern from reduced motion).

**Don't hand-roll a new `requestAnimationFrame` tween.** Call `mcCountUp()`
instead — it's the single place this decision is made, so any code that
reuses it is automatically correct.

If a future effect genuinely can't go through `mcCountUp()` (not a numeric
text tween), gate it explicitly:

```js
if(!MC_RM){ /* animate */ } else { /* jump straight to the end state */ }
```

## Why two mechanisms instead of one

A pure-CSS count-up (`@property` + `counter()`) was considered and rejected:
`counter()` only renders integers, but `mcCountUp()` also drives decimal
figures (the burn-rate readout uses 3 decimal places). A CSS-only version
would need a second, parallel implementation for decimals — more code, not
less, for a function that already funnels every call site through one
already-gated place.
