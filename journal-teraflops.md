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

(appended as work proceeds)
