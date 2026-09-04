# Journal — TFLOPS on the GPU and AI Models tabs

**Started:** 2026-08-17
**Branch:** `feat/gpu-teraflops` off `next` (v0.31.0)
**Ask:** "I want to see teraflops in GPU tab and AI models tabs. develop, test and
push to next branch, bump version on next branch."

## Before-state

- `next` @ 20371f4, VERSION = 0.31.0.
- GPU tab shows: util, VRAM, temp, fan, power, mem-BW util, core/mem clock,
  P-state, throttle spans, per-card cockpit + per-metric charts.
- AI Models tab shows: per-server VRAM timeline, per-model residency (weights vs
  ctx split), peak/avg, runs/spills, callers, live-serving chips.
- **Nowhere is compute throughput shown.** The dashboard can tell you a card is
  90% "utilised" but never how many FLOP/s that actually is, and the AI Models
  tab never says what compute a loaded model is sitting on.

Live fleet at start (from `ardi:9801`):

| host  | cards                    | VRAM   |
|-------|--------------------------|--------|
| local | Quadro P2000             | 5 GB   |
| vader | 3× NVIDIA GeForce RTX 3090 | 24 GB each |

## Design

`nvidia-smi` does not report FLOPS, and there is no counter for *achieved*
FLOP/s without profiling the kernels. So the honest thing to publish is
**theoretical peak**, computed from facts we can get:

```
FP32 TFLOPS = 2 × shader cores × clock(GHz) / 1000
```

The only missing input is the shader-core count, which `nvidia-smi` doesn't
expose. That comes from a small curated spec table keyed on the card name
(core counts are stable published facts). Cards not in the table publish
**nothing** — absent, not zero — matching this repo's existing honesty rule for
unsupported metrics.

Two numbers per card, both derived, no schema change:

- **peak** — at the card's boost clock (`clocks.max.sm`, else the table's boost).
  A capability figure: "what this box can do".
- **now** — at the clock the card is running *right now* (`clk_sm`). Because
  `clk_sm` is already in `gpu_samples`, this charts over history for free.

Also carried where the vendor publishes it: **dense FP16 tensor TFLOPS**, which
is the number that actually matters for LLM inference. Stored as an absolute
spec value (not an FP32 multiplier) because the GeForce FP32-accumulate halving
makes multipliers wrong on exactly the cards this project runs on.

### AI Models tab

Per server: the pooled compute of the cards that server holds VRAM on.
Per model: the same, scaled by the share of the model resident in VRAM — a
spilling model literally loses compute to the CPU, and that is the insight.
Flagged approximate in the tooltip.

## Log

**Backend.** `backend/gpuspec.py` — ~80 rows, `lookup()`, `compute_for()`,
`attach()`, `pooled()`. Read paths only; nothing stored, no migration.

- `/api/gpu/history`: per-card `compute`, `supports.tflops`, a `series.tflops`
  derived from the `clk_sm` column already in `gpu_samples`, `combined.tflops`
  summed across cards, and `now_pooled.compute`.
- `/api/data` + `/api/live`: both now go through one new `app.live_now()`. They
  had been reaching for `LATEST` independently, so the first attempt shipped
  compute on `/api/live` and not on `/api/data` — which is precisely the tab the
  feature was for. Caught by a test, not by looking.

**Things the tests caught that reading would not have:**

1. `radeon` was in the noise-word list, which made the key `radeon vii`
   unmatchable — "Radeon" is that card's whole model name. Removed from the
   list; `lookup()` searches for a key *inside* the normalised name, so leftover
   words cost nothing.
2. `attach()` originally skipped a card that already had a block. The fast lane
   mutates the *same* card dicts every 2 s, so `fp32_now` would have frozen at
   whatever clock the card held the first time the page was opened.
3. My own test asserted 53.2 where 71.0 × 0.75 = 53.25 → 53.3. The test was
   wrong, not the code.

**A regression I introduced and then fixed.** The sixth KPI tile made a 1400px
window resolve six 178px tracks — 146px usable, where the VRAM tile's
"33.5 GB / 72.0 GB" needs ~160 and wrapped onto a second line. Measured in the
browser at eight widths rather than guessed at; the grid's minimum track went
150 → 185px, which drops the last tile to its own row instead. 1100px, which
wrapped *before* this branch, no longer does.

## Verified live (ardi:9801, 2026-08-17)

| host  | cards            | FP32 peak | FP16 tensor | at idle clocks |
|-------|------------------|-----------|-------------|----------------|
| vader | 3× RTX 3090      | 106.8     | 213.0       | 13.2           |
| local | Quadro P2000     | 3.0       | *(none — Pascal)* | 0.28     |
| local | a retired "GPU 1" entry | *(absent)* | — | — |

The retired card is the case that matters: no name to match, so it publishes no
`compute` and `supports.tflops` is false, rather than a confident 0.

Per-card history charts back to `all` with no migration, as intended. The
by-metric view plots all three 3090s from ~4.4 T idle to ~35 T under load.

## Test results

- `tests/test_gpuspec.py` — 23 checks (matching, refusals, table sanity)
- `tests/test_gpu_tflops_api.py` — 11 checks (API wiring, derived series, pooling)
- `tests/js/test_tflops_cells.js` — 28 checks (models-tab arithmetic, spill path)
- Full suite on Python 3.13: **877 passed**, against a `next` baseline of 843.
  The 7 failures are identical to `next`'s and pre-date this branch
  (`test_public_status` ×5, `test_no_silent_swallow`, and the changelog snapshot
  — the last of which this branch rebaselines as part of the version bump).
