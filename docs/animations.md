# Animation conventions

The dashboard has a "Reduced motion" setting (Settings → General, backed by
`reduced_motion` in `SETTING_DEFAULTS`) so people who find the live-refreshing
gauges exhausting to look at can turn the movement off. It also respects the
OS-level `prefers-reduced-motion` automatically. Both feed a single JS flag,
`MC_RM` (`static/dashboard.html`), and a matching DOM attribute,
`html[data-reduced-motion="1"]`.

New animations must plug into one of the two mechanisms below — never invent
a third.

## 1. CSS transitions / `@keyframes` → nothing to do

Any animation expressed as a CSS `transition` or `@keyframes` is silenced
automatically by the global kill switch near the top of the `<style>` block:

```css
html[data-reduced-motion="1"] *,
html[data-reduced-motion="1"] *::before,
html[data-reduced-motion="1"] *::after{transition:none!important;animation:none!important}
```

This covers everything — present and future — with zero per-element opt-out.
If you add a gauge arc, a progress bar, a pulsing badge, anything driven by
CSS, it's covered the moment it ships. You don't need to check `MC_RM` in
JS for these; just use ordinary CSS transitions/animations as you normally
would.

## 2. JS-driven value tweens → route through `mcCountUp()`

Some effects can't be expressed in CSS — the cockpit's count-up numbers are
the one example today (`mcCountUp()` in `static/dashboard.html`, used for
every gauge percentage, the burn-rate figures, and the cost cards). It
manually steps `textContent` over a series of `requestAnimationFrame` calls,
and it already checks `MC_RM` (and `LIVE_TICK`, so a number doesn't
re-animate on every ~2s live refresh — a separate concern from reduced
motion).

**Rule: don't hand-roll a new `requestAnimationFrame` tween.** Call
`mcCountUp(el, target, duration, decimals)` instead. It's the single place
this decision is made, so as long as new code reuses it, new code is
automatically correct — there's nothing to remember per call site.

If a future effect genuinely can't go through `mcCountUp()` (not a numeric
text tween), gate it explicitly:

```js
if(!MC_RM){ /* animate */ } else { /* jump straight to the end state */ }
```

## Why two mechanisms instead of one

A pure-CSS count-up (`@property` + `counter()`) was considered and rejected:
`counter()` only renders integers, and `mcCountUp()` is shared with decimal
figures (cost cards use 3 decimal places), so a CSS-only version would need
a second, parallel implementation — more code, not less, for one function
that already funnels every call site through a single, already-gated place.
