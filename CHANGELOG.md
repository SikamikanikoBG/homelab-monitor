# Changelog

All notable changes to **HomeLab Monitor** are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), and the
project follows semantic-ish versioning. Each entry links to its full GitHub
release notes.

## [Unreleased] — `next`

**Added**
- **A configurable live-refresh interval, down to 1 second.** Settings → General → **Live refresh interval** picks how often the GPU tab and the Overview cockpit's live numbers update (1/2/3/5/10 s, default 2 s, matching the previous fixed cadence). Takes effect immediately, no restart — the background fast-lane loop re-reads it on its own next cycle. Never faster than the sample interval allows (a fast lane no faster than the sampler buys nothing and doubles the reads), and pinned off on a box that disabled the fast lane entirely via `FAST_INTERVAL=0`.

## [0.34.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.34.0) — 2026-09-04 · **Custom servers stop lying about what's loaded, and the cockpit stops blinking**
*A custom AI server registered against a remote fleet host had two separate ways of looking idle when it wasn't — its own live telemetry never carried the host tag to reach that box's card, and "loaded" was inferred purely from a VRAM figure every non-Ollama provider reports as absent. Both close out here. Separately, the Overview cockpit's arrival animation — patched twice already for one more repaint path each time — is gone outright: three independent, uncoordinated pollers repainting the same widgets made "gate it correctly" a fix that could never quite finish losing.*

**Fixed**
- **A custom AI server's live serving telemetry never showed up on the fleet host it was registered for.** Register one under Settings → Custom AI servers against a remote box (say a vLLM on another machine) and its tokens/sec, KV-cache and running/queued numbers stayed stuck on the hub's own AI Models panel instead of appearing on that box's own "AI models on {host}" card — `collect_serving()` scraped the telemetry correctly but never carried the same `fleet_host` tag the model list already used, and the registry had separately dropped the field needed to look a model row's telemetry back up. Both are threaded through now.
- **A custom server on a remote host showed "0 loaded" while visibly serving traffic.** Every probe except Ollama's reports `vram=None` for every model it lists — there's no on-disk/loaded split to report for a vLLM-style server, only "whatever it's currently serving" — so "loaded" was inferred entirely from VRAM attribution, and the one fallback for VRAM-less providers (the hub's own `nvidia-smi` process list) can never see a process running on a different box. A registered remote vLLM's own live `/metrics` telemetry — tokens/sec, running requests — is now cross-checked against the registry: a model whose server is confirmed to be actively serving is marked loaded regardless of whether a VRAM figure can be attributed to it.
- **The Overview cockpit's gauges and bars replayed their arrival animation on every refresh, not just the first paint.** The cockpit is repainted by three independent pollers — the live stream, the ~15-60s history/health poll, and the throttled cost refresh — and gating the sweep-in/count-up on "is this the first paint" kept missing at least one of them, so the hero gauge, the fleet rail's mini-bars and the leaderboard visibly reset and refilled on every refresh instead of just once. The arrival animation is gone entirely now; every cockpit value is set directly.

## [0.33.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.33.0) — 2026-08-27 · **Every model server, not just the ones we happen to know**
*The AI Models tab only ever saw servers it could guess at: the hub's own containers on their standard ports, and each remote's localhost ollama. The moment a vLLM lives on another box at a non-standard port — the normal way people actually run one — it was simply invisible, and there was no way to tell the monitor where to look. Now you can. Register a server by host, port and type in Settings and its models land in the AI Models tab, the installed-models registry and the MCP — with live serving telemetry where the server exposes it — while a small hardware fix means a multi-GPU box finally names every card in the System tab instead of quietly reporting only its first.*

**Added**
- **Custom AI servers — ollama, vLLM, llama.cpp, LM Studio, or any recognised provider, on any host:port.** A new **Custom AI servers** block in Settings → General: add a server by label, host, port and type, **Test** it (the probe runs from the hub, since the browser can't be assumed to reach a private host), and **Remove** it. The working set is a single persisted JSON setting, so the existing Save / autosave / validation paths handle it with no special-casing. A server that isn't a container and isn't localhost is exactly the gap this closes — a vLLM at `vader:8010` now shows up the moment it's registered, instead of never.
- **Its models flow through everything the fleet registry already does.** A registered server's models land in `model_catalog` and the live `models` list, so the **AI Models** tab, the **Installed-models** registry, the peak/avg/runs history and the MCP `/api/models` all pick them up with zero further plumbing. A registered vLLM also gets live **serving telemetry** (tok/s, KV-cache, running/waiting) because the port is threaded into the `/metrics` scrape — the same data the tab already draws for a container vLLM.
- **The type list is the real one, not a guess.** The dropdown is built from the monitor's own provider table, so whatever the auto-discovery already recognises is addressable by port — and a provider added to that table later becomes custom-addressable for free, with no second edit.

**Fixed**
- **The System tab's Hardware card names every card, not just the first.** A multi-GPU box reported only its GPU 0 (brand + model) under **System → Hardware**. The hub and both remote probes now ship a per-card list (brand, model, VRAM), and the card renders one row per card — `GPU 0 · NVIDIA GeForce RTX 3090 (24 GB)`, `GPU 1 · …` — on the hub, Linux remotes and Windows remotes alike. The legacy single-field path is kept for a client that only reads it. (Fan *count* was deliberately not added: `nvidia-smi` reports only an aggregate fan-speed % per card, which the GPU tab already shows — there is no driver source for a count, and a spec-table guess would be wrong for AIB cards.)

**Internal**
- **The two discovery paths share one normaliser.** `probe_models` (container, port-guessed) and the new `probe_custom_server` (explicit host:port) both run their raw rows through a single normalise-and-collapse step, so the "loaded models verbatim, oversized idle catalogue collapsed to a count" rule can't drift between them. The OpenAI-compatible providers are identified by a marker the probe factory sets, so the "which providers take an explicit port" set is derived from the table rather than re-typed — a source of silent rot otherwise.
- **A bad custom-servers setting degrades, it never breaks the sample.** The value is validated at the settings door (JSON, whole-number port in range, a known provider, no duplicates) *and* re-parsed defensively at read time; a malformed stored value is logged and treated as "no custom servers" rather than taking down a metrics sample. The one-shot Test endpoint always returns 200 and echoes only a model list, never a URL or credential.

## [0.32.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.32.0) — 2026-08-17 · **How many FLOP/s is that box actually worth?**
*The GPU tab could tell you a card was 100% "utilised" and never once say how much arithmetic that was worth. Utilisation is a time measurement — the fraction of the interval some kernel was resident — and it is the same 100% whether the card is doing 35 TFLOPS of matrix multiply or spinning on memory. TFLOPS now sits next to it on the GPU tab, on every card and on every host, and follows through to the AI Models tab where the interesting version of the question lives: what compute is this model actually sitting on, and how much of it does spilling into system RAM cost you?*

**Added**
- **TFLOPS on the GPU tab, per card and per box.** A KPI tile with its own sparkline, three chips (**FP32 peak** at rated boost, **FP32 at this clock**, and **dense FP16 tensor** where the cards have tensor cores), the card's rated figure beside its name in every per-card panel, and a **TFLOPS row in the small-multiples grid** — directly under utilisation, because they answer the same question at two different resolutions. It is charted in the **By metric** view too, so "which card is doing the work" is one click. On the 3×3090 box that means **106.8 TFLOPS FP32 / 213 TFLOPS FP16 tensor** as the ceiling, against whatever the current clocks allow.
- **The history came for free.** `clk_sm` was already stored per card per sample, and peak scales linearly with clock — so the whole time-series is derived on read from rows the database has had all along. **No new column, no migration, and it works retroactively over every range back to `all`.** It also means a card whose driver won't report a clock gets a peak figure but no series, rather than a boost-clock flat line drawn across an idle night.
- **The AI Models tab says what each model is running on.** A **Compute** column per model and a compute pill per server, resolved through the same per-card VRAM attribution the GPU cockpit uses — so "ollama is on GPUs 0 and 2" means the same thing on both tabs. FP16 tensor is shown where the cards have it, since that is the number that governs token throughput.
- **A spilling model is shown losing compute, not just VRAM.** The tab already flagged that a model didn't fit; it never said what that costs. A model 75% resident in VRAM now reads **53.3 of 71.0 TFLOPS** — the layers pushed into system RAM run on the CPU and get none of the card. Marked approximate in the tooltip, because the split is by bytes and layers differ in cost.

**Internal**
- **A card the table doesn't know publishes nothing, not zero.** `nvidia-smi` reports no FLOPS and there is no counter for achieved FLOP/s outside a profiler, so what ships is theoretical peak from `2 × shader cores × clock` — with the core count from a table of ~80 published specifications, since that is the one input no driver exposes. Unrecognised cards advertise the metric as unsupported, exactly like every other optional metric here. **Laptop and Max-Q parts are refused outright** rather than answered from their desktop namesake: an "RTX 4090 Laptop GPU" is 9728 cores against the desktop part's 16384, and a plausible wrong number is worse than a blank. Entries store each vendor's *published* peak rather than deriving it, because the derivation isn't uniform — RDNA 3 dual-issues FP32, and GeForce tensor cores halve their FP32-accumulate rate against the datacentre parts.
- 62 new checks: a Python suite over the spec table and the API (name matching, the refusals, the derived series, the pooled rules), and a second Node suite that lifts the AI Models tab's arithmetic straight out of the shipped HTML — the live fleet has one card and one fully-resident model, so the multi-card and RAM-spill branches would otherwise have shipped unverified.

## [0.31.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.31.0) — 2026-08-16 · **The charts caught up with the numbers**
*0.30.0 split the page into two cadences — live values on a two-second stream, history charts on their own timer — and the split quietly went wrong: every number moved every two seconds while every chart sat still for up to two minutes. Closing that gap turned up four separate ways the refresh loop could stop dead without anything on screen looking wrong.*

**Added**
- **Top processes works on every host, not just the hub.** The mini-htop card was hub-only because `/proc` was the hub's and the probe never shipped a process table. It does now: `probe.py` walks `/proc` twice around the **same sub-second dwell its CPU reading already takes**, so a remote gains a process table without its poll taking any longer than it did before. The maths is app.py's, line for line — grouped by command, CPU as a share of one core — because a number meaning "percent of one core" on the hub and something else on a remote is worse than no number at all. Two windows differ, though, and the card says which one you're reading: the hub measures across its whole refresh, a remote across the probe's dwell, which resolves busy processes well and idle ones coarsely. RAM is a straight read and is exact either way. Per-process disk I/O needs `/proc/<pid>/io`, which is root-only on most distributions, so a remote reports it unavailable rather than shipping an empty table. Hosts too old to send the block — and Windows remotes, which have no `/proc` — hide the card exactly as before.

**Fixed**
- **Charts move with the numbers again.** The history timer was pinned to the server's own bucket size — 60s on the default 6h range — and `applyLive()` only ever assigned the current values, never touching a chart series. A tile and the chart directly beneath it could disagree by two minutes. Each live frame is now folded into the newest bucket on the client, at **no extra request**, because the stream already carried every field it needed. Within a bucket the point keeps a running mean, seeded from how far into the bucket it already is, so it converges on the `AVG()` the server will send rather than jittering — and the next authoritative fetch barely moves it. Chart lag before this, measured by range: 1h ~25s, **6h ~120s**, 24h ~5min, 7d ~29min, `all` ~5.5h.
- **A GPU spike is a spike again.** VRAM is the only step metric on the page, so averaging it across a 60s bucket didn't merely delay a model load — it flattened the peak away. A load of 481 MB → 7900 MB now lands at 7900 immediately; the mean would have drawn ~4191. Rate metrics stay averaged, because for those the average is the honest number.
- **The per-card GPU panels refresh like everything else.** The small-multiples grid only repainted on the 60s history fetch, because the full render replaces the whole grid and re-binds every card's click and keyboard handler — it could not run on the live cadence without fighting the pointer and dropping focus mid keyboard-nav. Per-card temp, watts and utilisation are the fastest-moving numbers on the page, so the grid sat still while the detail chart behind it tracked live: same data, two different answers depending on which view you were in. Each card's body is now written in place and the wrapper — its handlers, tabindex and focus ring — is left untouched. Anything structural, a card appearing or retiring or changing status, still goes through the full render.
- **Four ways the refresh loop could stop dead.** A throw out of a synchronous renderer skipped the reschedule and killed the chain for the life of the page — and it was invisible, because the header shows a clock time rather than an age, so a three-hour-old page read as normal at a glance. A stream flapping on its 3s retry hint reset the 15s timer before it could ever fire: 300 seconds of flapping produced **zero** refreshes. `LIVE_ON` tracked connection events but never data arrival, so a dead-but-open stream left the cadence slow *and*, because the fleet poll is gated on it, stopped polling the fleet altogether. And a terminally failed `EventSource` was never cleared, so the page could not open a stream again for the rest of its life.
- **Chart overlays stopped lying.** The in-place chart update never reassigned inline plugins, and the GPU plugins close over the payload they were built from — so throttle bands and the VRAM capacity line stayed frozen at page-load state while the lines underneath them moved. The overview chart's cache key was labels-only, which held it still for a whole bucket at a time. A range switch now re-arms the poll instead of waiting out the remains of the old range's tick.
- **Switching to a remote host left the hub's data on screen.** Only the System card changed; **Top processes**, **Power & cost** and the **CPU/RAM/load chart** kept the hub's numbers under the remote machine's name. The bug had a peculiar shape: `renderTopProcs()` and `renderCost()` each open with a correct guard that hides them on a remote — and their only caller wrapped them in *the same condition*, so on a remote they were never called, the guard never ran, and the last local paint stayed. The chart had no guard at all; it plotted `/api/data`, which is always the hub. This only surfaced on the local → remote *switch*, never on a fresh load, which is how it survived. The hub-only cards now hide the moment the host changes rather than on the next tick.
- **The System tab's chart now draws the host you selected.** Per-host CPU, RAM and load were already being stored in `host_samples` on every poll, but nothing ever read them back, so the panel stayed hardwired to the hub's own series — fixing the chart meant adding the reader it never had. `/api/host_history?host=&range=` serves those rows, falling back to the hourly rollup once raw retention expires. A host with no history yet says so next to the canvas instead of drawing an empty chart, which is indistinguishable from a chart of zeroes.

**Internal**
- The dashboard has its first tests. `tests/js/test_refresh_loop.js` runs the real functions — lifted out of the shipped HTML, unmodified — against a fake clock: 65 checks, wired into CI on the runner's preinstalled Node, with no npm project and no new dependency. It was mutation-tested by re-introducing each original bug one at a time, and every one is caught by the check written for it. `tests/test_dashboard_refresh_invariants.py` guards the structure and now actually runs in CI, which had been invoking pytest on the snapshot suite alone.

## [0.30.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.30.0) — 2026-08-08 · **A dashboard that keeps up, disk scans on every box — and a container that had quietly been running itself twice**
*Live values went from twenty-five seconds stale to two, and the app asks the server for less than it did before, because the fix was to stop conflating how often something is measured with how often it is stored. Chasing a stream that ticked twice per interval then turned up something older and worse: every container ever shipped has been running two complete copies of the application.*

**Added**
- **The dashboard moves in real time now — and asks the server for less than it used to.** Screen cadence and storage cadence are separate things at last. `SAMPLE_INTERVAL` still governs what gets written, because every energy and cost figure is integrated as `sum(watts) × INTERVAL` and a denser sample would silently reprice your whole history. Alongside it, a new **`FAST_INTERVAL`** (2s, `0` disables) re-reads only the cheap counters, **writes no rows**, and wakes a server-sent-event stream; the heavy work stays on the sampler. `/api/data` — ~15 bucketed aggregates plus the entire charted series — is now fetched no faster than its own buckets can change. Live values: **~25s worst case → ~2s**. Requests over 65 seconds on a live box: **`/api/data` 4 → 1, `/api/fleet` 4 → 0**, and a backgrounded tab costs nothing at all.
- **Charts and KPIs refresh in place instead of the page being rebuilt.** Every refresh used to destroy and re-instantiate every chart on the page — which is also why the code had to hand-save and restore which datasets you'd toggled off. They're updated in place now, and count-up animations don't re-run on a live tick, because a number that re-animates every two seconds never settles.
- **Disk-content scanning works on every host, not just the hub.** Remote machines used to get a notice explaining that the Disks tab couldn't help them. Now a remote is scanned with the same `du` over the SSH connection the fleet is already polled on, with `df` along for free space — still nothing to install on the far end. Paths reaching a remote shell are validated and shell-quoted (there's a test that pushes each hostile path through a shell parser to prove it stays one argument), and Windows or unreachable hosts now say which of the two is the problem instead of offering a button that would fail.

**Fixed**
- **Every container was running two copies of the application.** The entrypoint starts the monitor as a script, so `app.py` is the module `"__main__"` — and every lazy `import app as _app` under `backend/` found nothing registered under `"app"` and executed the whole file a *second* time as a separate module object. That gave one process two SQLite connections and two `LOCK`s each guarding a different object. It also gave it two `LATEST` dicts, so the API served one while half the samplers wrote the other — and, because the worker threads start at module level, **two collectors, two host pollers, two notifiers**. Nothing ever crashed, because the second copy never reached the line that binds the port. What it did instead was **SSH-probe every registered remote twice per interval** and write every append-only table twice: on the v0.29.1 release build, **268 duplicate `(ts, service)` groups in `proc` and 10,663 in `net_samples` within a single hour**, hidden in `samples` behind `INSERT OR REPLACE`. Registering the module under its import name before the first backend import fixes it; duplicate rows go to zero, and the fleet is polled half as often as it was yesterday.
- **The star-history chart in this README rendered as a broken image.** `api.star-history.com` 301-redirects a mixed-case repo owner to its lower-case form, and GitHub's camo image proxy doesn't follow the redirect — it caches the empty 301. Lower-cased, with a comment so it doesn't get tidied back.
- **The README's "what's new" banner still announced v0.26** three releases later, and the configuration table was missing several variables that ship. The banner no longer names a version, so it can't go stale again.


## [0.29.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.29.1) — 2026-08-07 · **A container stopped for two weeks was reported using 1.25 TB**
*The fourth fix @andreahaku has landed here — this one found, diagnosed and fixed entirely from the outside, issue and pull request both. The Containers tab was charging a whole shared data directory to whichever container happened to name it.*

**Fixed**
- **Shared data is no longer billed to one container just because nobody else named the same path.** The check for "more than one container can write this" compared mount sources as strings, so it only ever caught two containers naming the same directory. It missed the nested case: one container mounting `/srv/models` while others mount `/` — which is what toolbox and distrobox do, at `/run/host` — write the same bytes under two different strings, so the entire tree was charged to the single container that named it. On the host that surfaced this, a container **stopped for two weeks** with a **40.9 kB** writable layer was reported at **1.25 TB**. Sharing is now decided by path coverage rather than string equality, and deliberately one-directional: mounting a parent doesn't cost you your data because someone else mounted a subdirectory of yours, so a container with 500 GB under `/srv/media` keeps being billed for it when another mounts only `/srv/media/photos`. Sources are normalised once for both the sharing check and the skip that consumes it — normalising only one side would make the skip miss in silence — and the collector logs a line when a parent mount takes data out of the count, because a disk column that empties itself without explanation is impossible to diagnose in the field. _(contributed by @andreahaku, #264/#265)_

## [0.29.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.29.0) — 2026-08-01 · **The GPU tab, on every box — per-card history, fan speed, and thermal alerts that ignore power caps**
*The hub got charts; every other machine got a snapshot. That was a storage limitation, not a UI one — per-card history simply wasn't kept for remotes. It is now, for every host, so one tab serves the whole fleet: a panel per card on a shared scale, fan speed (not collected anywhere in this project before), thermal-throttle windows shaded on the sparkline, and which card each service is sitting on. Alerts are sustained and per card, and a card pinned at a power limit you set yourself doesn't count as throttling.*

**Added**
- **The GPU tab is the same tab on every host now — with history, fan speed and per-card attribution.** The hub had charts; every other machine got a still photograph. That was never a rendering choice: `host_samples` pooled a box's GPUs into one row and threw GPU temperature away entirely, so a remote had nothing to chart, and the old panel's own caption admitted it. Per-card history is now stored for *every* host, hub included, in one table — so one renderer serves the whole fleet and the fork is deleted rather than maintained. What lands with it: **fan speed**, which this project has never collected anywhere (NVIDIA `fan.speed`, AMD `pwm1`/`fan1_input`); the deep telemetry the hub kept to itself (memory-bandwidth utilisation, core/memory clocks, power cap, memory-junction temperature, perf state, throttle reasons) now read on remotes too; and **which card a service is actually sitting on**, by mapping each compute process through its GPU UUID and its cgroup — so a 3×3090 box shows ollama's 63 GB split 22.5 / 22.1 / 18.8 across the cards instead of one pooled number.
- **One panel per card, laid out for spotting the odd one out.** Same rows in the same order, and **the same y-scale per metric across cards**, so a taller temperature line means a hotter card rather than an artefact of auto-scaling. Thermal-throttle windows are shaded on the sparklines themselves, so *when it started* needs no click. A **By metric** view transposes the same data onto one axis when the question is "which card is the problem" rather than "what is card 1 doing". Above them, pooled KPIs each carry their own sparkline; below, all cards combined as stacked VRAM against the box total with the hottest card overlaid — **maximum across cards, never the average**, because an average over one card at 87 °C and two idling at 45 °C reads as a comfortable 59 °C and tells you nothing.
- **Who is using the GPUs, over time and in watts.** Per-service VRAM history for any host, plus an energy and cost figure per service — each card's measured draw above its idle floor, shared out in proportion to the VRAM each service holds. It is labelled an estimate everywhere it appears, because GPUs meter power per card and never per process, and the idle floor is reported as its own band rather than billed to whichever service happens to be resident.
- **Alerts for cards that are throttling, overheating, or have lost a fan.** Per card and per host, so one hot GPU neither hides nor implicates the others. Every threshold is paired with a duration — a card touching 85 °C for two seconds mid-batch is not an incident, and a tool that pages for it gets muted inside a week. Clearing uses hysteresis so a card hovering on the line doesn't flap. **Power-capping is never an alert**: a box whose cards run at a deliberately lowered power limit sits at its cap by design, and that is reported separately as *Capped*. Per-host threshold overrides ship with the feature for the same reason. The message names the cause — which service holds VRAM on that card, and whether the fan has any headroom left, because "fan already at 100%" is the difference between *turn the fans up* and *you are out of cooling*.
- **Every host reports its AI models now — not just the hub, and not just ollama.** The POSIX probe reads any recognised server answering the OpenAI-compatible `/v1/models` on its standard port (vLLM, llama.cpp, LM Studio, tabbyAPI, xinference, SGLang, LiteLLM, koboldcpp, Aphrodite, Infinity, Cortex), so a remote box running something other than ollama stops being a blank tab. Three guards keep it cheap and honest: only ports already listening are touched (from the `ss` data the probe collects anyway, so the usual cost is zero extra connections), a reply counts only when it parses as the documented shape — an unrelated web server on `:8080` is never reported as a model host — and models are listed **Idle**, matching how the hub treats its own non-ollama servers. The Windows probe gained the ollama read it never had (`win-0.2`), so Windows boxes now appear with the same size/quant/params detail as Linux ones.

**Fixed**
- **Stacked service charts were rendering as one solid black block.** Service colours were generated as `hsl(...)` while several chart call sites append a two-digit alpha suffix — and `hsl(210 62% 56%)cc` is not a colour, so Chart.js fell back to opaque black. Colours are hex now, and the hash that picks them was scattering badly enough to give two services on the same chart near-identical purples; it spreads them properly.
- **The AI Models tab now answers for the host you actually clicked.** Picking a remote host and opening AI Models could show "No AI models reported on *host* yet" while the API had that host's models all along. The panel's 15-second throttle was armed by host-switch calls that never painted anything — every host switch fires the tab's renderer whether or not you are looking at it — so the click that followed was silently skipped, and recovery was left to a poll tick running on exactly the same 15-second period. Switching hosts could also strand the previous host's card on screen under the new host's name. The panel is now cached per host (one fleet-wide fetch warms every host you might click next), stamped with the host it was drawn for, and shows *"Reading host's model servers…"* until an answer actually arrives — it never claims a host is empty before it has asked. The **Installed models** card follows the host selector too, instead of listing the whole fleet under one host's name, with an **All hosts** tick-box when you do want the fleet view.
- **A host whose registered name differs from its hostname is no longer invisible here.** Remote catalogue entries were keyed by whatever the probe's own `socket.gethostname()` returned, while the dashboard filters by the name the host is registered under — so a box added as "Work" that calls itself "DESKTOP-…" never showed a single model, with no error anywhere. Entries now carry the registered name.

## [0.28.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.28.0) — 2026-07-31 · **Where your VRAM actually went — weights vs context, who's holding the model, and AMD cards that report like NVIDIA ones**
*A loaded model stops being one opaque number. You can now see the split — weights vs context/KV — how much spilled into system RAM, how many of its runs spilled, and which app is actually driving it. The AI Models tab refreshes in seconds instead of half a minute. And AMD boxes reach NVIDIA parity: per-process VRAM read straight from the kernel's own DRM fdinfo nodes, real card names, clocks, perf level, power cap and memory temperature. Half of this release came from outside the repo — thank you **@andreahaku**, twice over. It also folds in the mcp SDK pin that was bumped to 0.27.1 but never actually released.*

**Added**
- **A model that spills into system RAM is now visible everywhere — with how often it happens and which app is driving it.** A model that doesn't fully fit VRAM quietly runs its overflow layers on the CPU; outside the Benchmark Lab that was invisible. The ollama probe now keeps `/api/ps` `size` next to `size_vram` (the difference is the spill), so the AI Models tab shows a **Loaded · RAM spill** badge and the live VRAM + RAM split, a **Runs** column counting contiguous load sessions and how many of them spilled, and per-model **Used-by** pills naming the callers — attributed by time-overlap of the caller↔server connection samples with the model's residency, which finally answers "which app is holding this model" when one server hosts several. The GPU tab gains a warning banner while something is spilling right now, the Insight Feed logs it, and a `homelab_model_ram_spill_mb{server,model}` gauge exports it. A fully-CPU model (`size_vram=0`) now correctly reads **Loaded** instead of collapsing to Idle. (#243)
- **AI Models: a loaded model's memory is now split into *weights* vs *context/KV* — the answer to "why did it spill into RAM".** The Now cell gains a stacked split bar and caption (e.g. "weights 17.3 GB · ctx 3.4 GB"): weights are the model file itself (ollama `/api/tags` size), and the rest of the residency is the context/KV cache + buffers — the part that grows with the context window. The metadata chips now show the **runtime context** the load is actually running with (`@ 32K ctx`, from ollama's `/api/ps context_length`, model max in the tooltip), and the RAM-spill insight explains the split in words — including that a smaller context window shrinks the KV cache and may fit VRAM. No DB change; older ollama servers simply omit the runtime-ctx chip. (#244)
- **AI Models tab refreshes in seconds now.** A new light `GET /api/ai/now` endpoint (no DB, no lock) re-probes just the ollama servers on demand — throttled server-side to once per ~3s — and the tab polls it every 5s while visible (local host, auto-refresh on). Loads, unloads, spill and ctx changes show up in a few seconds instead of the previous ~25s worst case (10s sampler + 15s global poll). (#244)
- **AMD per-process VRAM is now read from the DRM fdinfo nodes, so "VRAM by service" names real services on AMD boxes.** amdgpu has no `--query-compute-apps` equivalent, but the kernel already publishes per-client residency in `/proc/<pid>/fdinfo/<fd>` — so the GPU tab goes from one anonymous `system/other` band to a row per container, and "Services on the GPU" fills in. Measured on a Ryzen AI Max (Strix Halo, 124 GiB GTT): **99.9% of the kernel's own reported GTT attributed, in 8 ms across 550 processes / 1621 fds**. Correctness the naive version misses: a bare fdinfo number is *bytes* not KiB, fd identity comes from the DRM device number rather than the `/dev/dri/` path (so a container that remaps its render node keeps its attribution), and `drm-shared-*` is subtracted so one dma-buf isn't billed to two clients at once. _(contributed by @andreahaku, #247)_
- **The GPU panel reaches NVIDIA parity on AMD cards — clocks, perf level, power cap, mem-bandwidth and memory temp.** All read from amdgpu sysfs in the same per-card pass (starred `pp_dpm_*` rows, `mem_busy_percent`, hwmon `power1_cap`, `power_dpm_force_performance_level`, the hwmon channel labelled `mem`), each field optional so a card that lacks one simply hides that chip. APUs expose no `product_name`, so a Radeon 8060S that used to read "AMD GPU 1" now gets its real name from the host's `pci.ids`. Rode along: the pooled power chip could read **over 100%** on a hybrid box, because total draw across both vendors was being compared against the NVIDIA-only cap — the aggregate is now built across vendors, and publishes a cap only when every card actually reported one. _(contributed by @andreahaku, #254)_

**Fixed**
- **The built-in MCP server no longer crash-loops after a rebuild.** The dependency was pinned as an open-ended `mcp>=1.9.0`, so any rebuild after the SDK's 2.0.0 release pulled in a version that had removed the legacy `mcp.server.fastmcp` module the server imports — `ModuleNotFoundError` on import, in a restart loop. Pinned to `mcp>=1.9.0,<2`. This shipped as a `0.27.1` version bump that was never tagged or published, so it reaches everyone here for the first time. (#259)

## [0.27.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.27.0) — 2026-07-28 · **The fleet release — every GPU on every host, models fleet-wide, CPU power measured, and hosts you can rename**
*The GPU tab stops being hub-only: a remote box now shows every card it has — per-card VRAM, utilisation, power and temperature, plus the processes holding the memory (a 3×3090 rig used to read as a single idle 24 GB card). The AI-model registry goes fleet-aware. CPU & DRAM package power lands via RAPL. AMD hosts get per-process VRAM attribution. And a host added with a typo'd name can finally be renamed instead of deleted. Two of the headline features came from outside contributors — thank you **@1HazyOne707** and **@pehota**.*

**Added**
- **Per-host GPU view — every card, not just card 0.** The remote probe now ships a per-card list (index, name, utilisation, VRAM, power, temperature — the same fields the hub records for its own cards) plus nvidia-smi's compute-process list, and the GPU tab renders the real thing for a remote host: VRAM/util KPIs, per-card bars, and a **Processes on the GPU** table (a process sharded across several cards is pooled into one row, not listed once per card). The host-level aggregate now pools all cards the way the hub pools its own — VRAM & power summed, utilisation averaged, temperature = the hottest card — so a 3×3090 box finally reads 72 GB, not 24. Data rides the fleet payload the page already polls (no new endpoint); hosts on an older probe keep the capability notice. Per-host GPU *history* and service attribution need per-host storage — that's the next slice. (#252)
- **The AI-model registry is fleet-aware.** "Installed models" no longer collapses the same model living on two machines into one row: the registry dedupes by (model, provider, host), groups by host → provider, stays visible on remote-host tabs, and — for ollama — collects each remote's on-disk catalogue over the same SSH channel the poller already uses. A thundering-herd race in the registry cache (a burst of concurrent callers each re-fetching a stale cache) is fixed on the way, with double-checked locking. _(contributed by @1HazyOne707, #236)_
- **CPU & DRAM package power, measured via RAPL.** Hosts with Intel/AMD RAPL support export per-package CPU and DRAM wattage as Prometheus gauges (`homelab_host_cpu_power_w` / `homelab_host_dram_power_w`) — measured silicon power next to the GPU's, not an estimate. The shipped compose file now mounts `/sys/class/powercap` so the counters are readable inside the container; hosts without RAPL simply omit the metrics. _(contributed by @pehota, #248)_
- **Rename a host in place.** A host registered under the wrong name could only be deleted and re-added — losing its poll calibration and splitting its experiment/benchmark history. The Hosts tab now has a rename pencil next to the name (and `PATCH /api/hosts/<name>` accepts `{"name": ...}`): the row moves with its SSH target, tags, calibration and last check; the poll cache is re-keyed so the host never shows "no data yet"; and runs/benchmarks recorded under the old name follow. `local` stays reserved, and a combined rename + invalid-target request changes nothing at all rather than half-applying. (#251)
- **AMD per-process VRAM attribution via DRM fdinfo.** On AMD hosts the GPU tab knew the card's totals but not *who* was using them — "GPU is idle." while utilisation spiked. The collector now reads per-process VRAM/GTT straight from DRM fdinfo (kernel 5.19+, no ROCm tools), dedupes dup'd file descriptors, applies the APU-vs-discrete GTT policy per PCI device on hybrid boxes, and feeds the existing attribution pipeline — so the VRAM-allocation bar, VRAM-by-service chart, container VRAM column and GPU cost attribution all light up on AMD too. (#249)

**Fixed**
- **The container log drawer no longer dies when logs go quiet.** Following a container whose logs paused past the heartbeat window killed the stream with a traceback — Python's chunked-response reader can't resume after a socket timeout. The log stream now speaks raw HTTP/1.0 over the Docker socket, so a quiet-log heartbeat simply reads again, and a closed browser tab tears the socket down cleanly. (#250)

## [0.26.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.26.0) — 2026-07-25 · **The LLM Benchmark Lab — tokens/sec, VRAM fit and the right context size, measured on your own cards**
*"Will it fit, and at what context?" stops being guesswork. The new Benchmark Lab loads your local models and measures what they actually do on your GPUs — generation & prompt tokens/sec, load time, the VRAM↔RAM split, and the largest context that still fits fully in VRAM — per card, priced by the same tariff engine as the rest of the app, and stored so you only re-run when something changes. Plus an external contribution: unified-memory APUs now report their real usable VRAM (GTT), not the 512 MB carve-out.*

**Changed**
- **Benchmark Lab results are now a compact, filterable, sortable table instead of cards.** Cards don't scale — with dozens of runs they're hard to scan. The results are now one table: one row per run with the model, setup (which GPU), gen/prompt tokens/sec (with an inline speed bar), load time, fit, max-VRAM/recommended context, the VRAM↔RAM split, energy and cost, and when it ran. Click any column to sort, type in the filter box to narrow by model/setup/fit, click a row to open its context sweep, and tick rows to overlay them in Compare. Running/failed rows show their status inline; the "weights spread onto a smaller card" hint moves to a ⚠ on the Fit cell (and a banner in the sweep).

**Fixed**
- **Benchmark Lab: a too-big context no longer hammers the box, and progress keeps reporting across tab switches.** Two issues from real use: (1) sweeping ascending contexts kept trying ever-larger sizes even after one had already failed to fit — so a 30B model under memory pressure would OOM (ollama HTTP 500) on 64k, then 128k, then 256k in a row. The sweep now stops at the first context that can't be allocated (larger ones can only fail too), and the opaque "HTTP 500" is reported as "context too large for available memory — ollama couldn't allocate the KV cache", with the sweep chart calling out the ceiling. (2) The running-job progress didn't visibly advance after switching tabs or reopening the dashboard: the poll is now independent of which tab is showing (so it keeps ticking and is current the instant you return), shows elapsed time, survives a render error, and no longer flickers the charts each tick. The per-generate timeout is also capped lower so a single stuck generation can't hold the single-flight slot for long.
- **Reloading the dashboard while on the Benchmark Lab no longer leaves a dead page.** Opening the dashboard with `#benchmarks` in the URL — or with the Lab as your last-used tab — threw a `ReferenceError` during init (the Lab's renderer lives in a later script block than the tab switcher) and killed event wiring and the poll loop before they started. The call is now guarded and the Lab renders itself when its block loads. (#245)
- **Unified-memory APUs now report their real usable VRAM, not the 512 MB carve-out.** On AMD APUs (a Ryzen iGPU driving a mini-PC, say) the dedicated "VRAM" is just a small carve-out — the GPU actually works out of shared system memory (GTT). The amdgpu reader (hub *and* remote probe) now reports the GTT pool when the card looks like an APU (≤ 1 GiB dedicated VRAM next to a large GTT pool — think Ryzen AI Max / Strix Halo), so an APU shows its true capacity instead of a permanently-full 512 MB; discrete cards keep reporting VRAM exactly as before. *(contributed by @andreahaku, #237)*

**Added**
- **Benchmark Lab: pick which GPU(s) to benchmark on, see the setup in every result, and overlay runs to compare.** Three additions to the Lab: (1) **GPU device selection** — since ollama has no per-request device choice, choosing a card spins up a *throwaway* ollama container pinned to exactly those GPU(s) (reusing your existing models volume, on a scratch port), runs the sweep, and tears it down — your main ollama is never touched. This is how you answer "all VRAM vs which one": benchmark the same model on each card and see. (Needs controls enabled, since it launches a container.) (2) **The setup is recorded and shown** on every leaderboard row and card (e.g. "⚙ RTX 3090" / "Quadro P2000"). (3) A **Compare view** overlays any set of stored runs on one chart — tokens/sec and VRAM across context — so e.g. the same model on a small card vs a big one sits side by side.
- **Benchmark Lab — a new AI tab that measures what your local models actually do on your GPU, and remembers it.** Pick one or more ollama models and a set of context sizes; the Lab loads each model and runs a short generation across the ladder, recording **generation & prompt tokens/sec**, **load time**, the **VRAM↔RAM split** (how much spilled to system RAM, straight from ollama's `size` vs `size_vram`), the **largest context that still fits fully in VRAM** — the recommended cap — a **fit verdict** (fits VRAM / spills to RAM / CPU), and the **power, energy and cost** of the run (priced by the same tariff engine as the rest of the app). On a multi-GPU box it also attributes which card the weights landed on and warns when a model spreads onto a smaller/slower secondary card. Results are **stored so you only re-run when something changes**, with a one-click re-run; the tab shows a speed leaderboard, per-model cards, and a context-sweep chart (tokens/sec and VRAM vs context). Benchmarking is an active, opt-in operation (it loads models and runs inference) and is single-flight so it never stampedes the GPU. New endpoints `GET/POST /api/bench`, `GET /api/bench/<id>`, `GET /api/bench/targets`, `POST /api/bench/cancel`, `DELETE /api/bench/<id>`; new MCP tools `get_benchmarks` / `get_benchmark`. Off automatically when no ollama endpoint is reachable (`BENCH_ENABLED`, `COPILOT_OLLAMA_URL`).

## [0.25.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.25.0) — 2026-07-12 · **AMD GPUs detected reliably, history retention you can set, and the maintenance badge reaches the public page**
*Pure-AMD and hybrid AMD+NVIDIA hosts now detect correctly — and an AMD box is no longer told to install the NVIDIA runtime. History retention becomes a live setting you change without a restart. The public status page shows the same "In maintenance" badge as the private dashboard. Plus a hardening pass: a Prometheus gauge double-registration crash, a pruning-cycle deadlock, and a controls gate on the remote host-run route.*

**Added**
- **History retention is now a setting, changeable live.** A new **General** settings tab exposes `retention_days` (1–3650); the pruning cycle reads it each pass, so a change takes effect within ~6 minutes without a restart. (#233)
- **The "In maintenance" badge now shows on the public status page.** Previously only the private dashboard dimmed alerts and showed the blue *In maintenance* badge during a planned window — the shareable `/public` page still showed a bare down state. It now computes `in_maintenance` through the same `_in_maintenance()` helper and surfaces a dedicated `maintenance` state when nothing is actually down. (#231, follow-up to #217)

**Fixed**
- **AMD GPUs are now detected reliably — including on hosts without nvidia-smi, and alongside an NVIDIA card.** Two gaps in the amdgpu back-end (#1): the collector called nvidia-smi unguarded, so on a machine without it the GPU read aborted before the AMD sysfs reader ever ran — a pure-AMD box reported "no GPU" even though the reader worked; and detection was NVIDIA-*or*-AMD, hiding the AMD card on hybrid machines. nvidia-smi is now read defensively and both back-ends always run, with their cards merged (AMD re-indexed so per-card history stays distinct) and VRAM/util/temp aggregated across all of them; per-process attribution still applies to the NVIDIA cards only. Utilisation also survives the amdgpu `gpu_busy_percent` EBUSY quirk — the intermittent "Device or resource busy" race — by retrying once instead of reporting 0%.
- **GPU diagnostics are now vendor-aware — an AMD host is no longer told to install the NVIDIA runtime.** The local requirements panel (and the remote-host probe's GPU check) hard-coded an "NVIDIA GPU" row with nvidia-ctk-only remediation, so a machine with a working AMD Radeon — read via the amdgpu sysfs back-end since v0.21.0 — still showed a confusing "no NVIDIA GPU detected" message. The row now reports the real vendor (GPU (AMD) / GPU (NVIDIA)), and the no-GPU remedy explains that AMD is detected automatically from the kernel (no ROCm needed), with a one-liner to confirm the card's sysfs nodes exist. (#1)
- **Prometheus gauges no longer crash on a double import/reload.** Gauges are cached in a module-level `_GAUGES` dict, preventing the `ValueError: Duplicated timeseries` on the Flask debug-reloader or a double import. (#233)
- **A pruning-cycle deadlock is fixed.** `sample_once` read the retention window while already holding `LOCK`, and `get_settings()` is itself non-reentrant on `LOCK` — the retention read now happens outside the lock. (#233)
- **Hardening round (#233):** the remote `POST /api/hosts/<name>/run` route is now gated behind `ENABLE_CONTROLS` (returns 403 when controls are off); the docker policy refresh used the wrong TTL constant (`_DOCKER_ENRICH_TTL` → `_DOCKER_POLICY_TTL`); container self-ID detection now reads the full cgroups-v1 ID from `/proc/self/cgroup` and falls back to `HOSTNAME` for v2/non-container; and changing a container's restart policy now asks for confirmation first.

## [0.24.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.24.0) — 2026-07-09 · **A restructured engine underneath, and controls on by default**
*The ~7,600-line `app.py` monolith is now a proper `backend/` module tree — behavior unchanged, backed by a 71-snapshot test suite and a CI gate that fails the build on a silent `except: pass`. Separately, container/service start-stop-restart controls (plus self-update) flip from opt-in to on by default — worth a glance at your compose file before you upgrade.*

**Changed**
- **Container/service start-stop-restart controls, and self-update, are now on by default.** Previously both needed explicit opt-in (`ENABLE_CONTROLS=1`, `ALLOW_SELF_UPDATE=1`). The shipped `docker-compose.yml` now mounts the Docker and D-Bus sockets read-write by default so both write paths work out of the box. Want the old fully read-only posture back? Bring the stack up with the new `docker-compose.readonly.yml` override, or set either flag to `0`. Containers: local host only for now. Services: local systemd, remote Linux/Unix (SSH + systemctl), remote Windows (SSH + PowerShell). Buttons stay disabled with an explanation when the underlying sockets aren't present; a real failure (permission denied, non-admin Windows session) surfaces the actual error, never a generic one. (#229)

**Added / Internal**
- **`app.py` extracted into a `backend/` module tree.** All 54 routes, every collector, probe and notify path, and all DB access now live in `backend/api/`, `backend/collectors/`, `backend/probes/`, `backend/notify/` and `backend/db/` (thread-local connections, a versioned migration runner, repo shims instead of raw SQL scattered everywhere) — HTTP responses byte-identical (71-snapshot suite). New rollup tables (`samples_1h`/`net_samples_1h`) make long-range Costs/heatmap queries cheap. Alert edge-state (`_NOTIFIED`, uptime down-since) now persists to the DB, so a restart no longer risks a duplicate or missed alert. Each background worker runs in its own thread with a watchdog that logs a stall instead of hanging silently. _(backend architecture refactor by @pehota, #230)_

## [0.23.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.23.0) — 2026-07-03 · **A real Disk I/O dashboard, AI models across every provider, and alerts that shut up during maintenance**
*The Disk I/O tab goes from a bare read/write table to per-device utilisation, op-latency, 6h trend sparklines, z-score anomaly detection and per-process attribution. The AI Models "Installed models" registry now catalogs vLLM/llama.cpp/LM Studio alongside ollama, grouped by provider. Tokens/sec gets first-class treatment on the Experiments tab and over MCP. Click-to-copy reaches every table in the dashboard, not just the System tab. And maintenance windows let you silence alerts for planned work instead of eating the noise.*

**Added**
- **Installed-models registry now spans providers, not just ollama.** The AI Models tab's "Installed models" panel merges ollama's on-disk catalogue (size, params, quant, last-modified) with every other recognised server's model list — vLLM, llama.cpp, LM Studio, ComfyUI, and the rest of the fleet's `PROBES` table — grouped by a new **Provider** column, with a filter box to search by model or provider. Surfaced via a new MCP tool `get_installed_models()` and a `homelab_models_installed_total{provider=...}` Prometheus gauge, so "what can I run, and where" no longer means SSHing in. Cross-host dedupe stays a follow-up. (#219)
- **Click-to-copy, extended beyond the System tab.** The one-click copy affordance from the System/Hardware cards is now a small reusable `copyable()` chip used across the dashboard: container names (Containers), unit names (Services), interface addresses/MAC (Network), listening-socket bind addresses/ports (Network), and model names (AI Models — both the Loaded view and the Installed-models registry). One delegated click listener now handles every copy button in the document, so new tables get the affordance for free without a per-render wiring call. Normal text selection is unaffected. (#220)
- **Tokens/sec on Experiments, first-class instead of buried — in the UI *and* MCP.** A run's `tokens_per_sec` (however your script named it — `tok_per_sec`/`tokens_sec`/`tok_s` all recognised) now gets its own chip in the Runs table instead of being lost among arbitrary metrics, leads the run-detail chart list, and — mirroring the AI Models tab's "Tokens / Joule" — the run's cost caption now reads out average/peak tok/s and **tokens/kWh**. `get_experiments()`/`get_experiment()` over MCP already passed it through (the run-tracking API stores any metric key you log, no schema change) — now documented explicitly so an agent knows to look for it, with test coverage proving the passthrough. Plus a documented `homelab_run.py` example. (AI Models tab's own tokens/sec, from vLLM/TGI `/metrics`, was already shipped and verified working — Ollama has no passive way to expose it, confirmed via its API design; that stays a known gap, not silently faked.)
- **Disk I/O Throughput, from a bare read/write table to a real per-device dashboard.** Each device now shows utilisation % and average read/write op-latency (extra `/proc/diskstats` fields), a 6h read-vs-write trend sparkline riding the same `/api/data` range everything else uses, a per-device z-score anomaly badge (rolling baseline, edge-triggered into the Insight Feed so a persistent spike logs once, not every scan), and per-process "top writer / top reader" attribution from a bounded `/proc/<pid>/io` sample of the processes already tracked for CPU/RAM (never a full-`/proc` scan). The headline Total read/write KPIs now sum **physical whole-disks only** — RAID/dm aggregates and partitions no longer triple-count the same bytes. Built with the app's own components throughout (`.kpi`/`.dbar`+`sev()`/`.pill`/`.badge`/`.mspark`) — no new visual language. Per-process attribution is authed-only, comm name only, never on the public status page.
- **Maintenance windows — silence alerts for planned work instead of eating the noise.** A new `maintenance_windows` table lets you cover a container, systemd unit, uptime check, disk or GPU (by name/id pattern, or `*` for everything) for a one-off, daily or weekly window; alerts routed through `_emit()` and uptime's down/recovery/cert/slow checks are silenced while a match is active, and the Uptime tab shows a blue **In maintenance** pill instead of a false "down". New `GET/POST /api/maintenance` and `DELETE /api/maintenance/<id>`. _(contributed by @1HazyOne707, #167)_

**Fixed**
- **Copy buttons silently claimed "failed" even when the copy worked.** A duplicate `copyText()` declaration shadowed the original (JS hoists the later one), which meant the standalone copy buttons (Share link, integration key, MCP command, update command, per-OS remediation command) always read back `undefined` from a function that never returned a value — showing "⚠️ Copy failed — select and Ctrl+C" on every successful copy. Consolidated to one `copyText()` that returns whether the copy actually worked. (#220)
- **Daily Brief on chat channels was a starved, misleading blob — rebuilt.** The brief only ever sent its rich card to **email**; Discord/Telegram/Slack/ntfy got a stripped text summary that (a) showed the headline twice, (b) was always coloured info-blue regardless of severity, (c) said "Services: 1 failed" without naming the service, and — worst — (d) **counted an offline host as up** (`5/5` while one machine was down), because it read each host's stale stored *Test* result instead of the live online flag the dashboard uses. Now: the fleet tally comes from the same `_host_is_online()` source as `/api/fleet` (offline hosts are correctly counted and show their last error); chat messages are coloured by real severity; action lines **name** the failing container/unit (e.g. `immich_ml — Exited (137) 2 hours ago`); the headline isn't repeated; and **Discord posts the full HTML brief as an attachment** alongside the clean summary. All "infant-icon" emoji are gone — status is shown with colour (CSS dots, the embed stripe) and quiet uppercase labels. (#170)

## [0.22.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.22.1) — 2026-06-27 · **Hero engine GPU gauge tells the truth about a full card**
*A small fix so the Overview's "Lab's Engine" GPU gauge stops reading calm when the card is actually maxed.*

**Fixed**
- **Overview engine GPU gauge now reflects VRAM saturation, not just compute util.** A GPU with models loaded but no inference running (low util, ~full VRAM) was showing a calm low number on the hero engine gauge while the GPU tab and the Overview status tile both correctly flagged it **crit** at ~94% VRAM. The gauge now reads the *binding constraint* — `max(util%, VRAM%)` — so a memory-pinned card lights up to match the rest of the UI, with both VRAM and `% util` shown in the line beneath.

## [0.22.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.22.0) — 2026-06-27 · **A real status page — public lab health, plus a page per service**
*The throwaway `/public` page is reborn in the app's own skin: overall lab health, a live list of the services you watch, and a dedicated status page for each one — uptime over 24h/7d/30d/90d, incident history and response times. Alerts also gain email, Slack and generic-webhook channels, services get brand logos, and the AI Models tab now lists every model pulled to disk.*

**Added**
- **A proper status page — overview + a page per monitored service.** The public page (`/public`) is rebuilt in the app's own design system (it was a bare throwaway before): the lab's system health *plus* a list of the services you monitor on the Uptime tab, each with a live heartbeat, 24h uptime and latency. Click any service for its **own status page** (`/public/<id>`) — current state and how long it's held, uptime over **24h / 7d / 30d / 90d**, a day-by-day history bar, a response-time chart, and a reconstructed list of past incidents (when it went down, for how long, and why), plus TLS-cert days-remaining. Each Uptime check has a new **"public" toggle** (off by default) so you choose exactly what's listed; the whole page stays gated behind `PUBLIC_STATUS`, and only the service name + host are ever exposed — never the raw target or credentials. New read-only endpoints: `GET /api/public-status` (now includes the monitors) and `GET /api/public-status/<id>`. _(public-status groundwork by @siva23367, #197)_
- **Daily Brief — an opt-in once-a-day HTML health digest.** A single scheduled email/Discord/ntfy/Telegram summary of the lab's last 24h — overall health, anything that alerted, and the headline numbers — so you get one calm daily readout instead of watching the dashboard. Off by default; pick the channel and the send-time. (#170, #207)
- Alerts now support **email (SMTP)**, **Slack incoming webhooks**, and a **generic webhook** target alongside Discord, ntfy and Telegram. All three channels configure from the Alerts settings panel and honour the existing minimum severity + disk threshold rules. _(contributed by @Jishnu-Prasad888, #191)_
- **Per-service notification rules — choose which services can page you.** Instead of every check shouting on every channel, each monitored service can opt in/out of alerting independently, so noisy or low-priority services stay quiet. _(contributed by @Mr-Neutr0n, #24/#151)_
- **TLS-certificate expiry tracking on uptime checks.** HTTPS checks now read the certificate's days-remaining and warn **before** it expires (surfaced on the dashboard and the new status pages), so a silent cert lapse doesn't take a service down. _(contributed by @1HazyOne707, #163/#198)_
- **Brand logos for famous services** on the Containers and Services tables. The monitor now shows the recognisable icon (Immich, Plex, Pi-hole, Home Assistant, Postgres, Grafana, n8n, Ollama, and ~60 more) in front of the name, matched from the container image or unit name — faster visual scanning of a busy host. Logos are embedded Simple Icons (MIT), so there are **no runtime external requests**; near-black brands fall back to the theme text colour so they stay visible in dark mode, and unrecognised entries are unchanged.
- **Installed-models registry on the AI Models tab.** A new panel lists every model **pulled to disk** on this host's local LLM (ollama) — name, on-disk size, params · quantization, a **Loaded** badge (with live VRAM) for models resident right now, and last-modified — with an *N models · X GB on disk · M loaded* header. Read-only (`GET /api/models` → ollama `/api/tags`+`/api/ps`, cached ~45s, always-200 graceful-degrade when ollama is off, no secret leak). Loads on tab view + manual refresh.
- **Copy buttons on the System & Hardware cards** so the OS / kernel / CPU / host values are one click to clipboard (with a clean fallback when the clipboard API isn't available). (#208)

**Changed**
- Wired the new Alerts form labels to the locale files so translated dashboards automatically pick up the email/Slack/webhook copy.
- **Overview engine gauges**: the `%` now rides inline on the number's baseline (reads as `67%`) instead of as a detached superscript, and the **GPU gauge shows live power draw (W)** on its own line beneath the VRAM.

**Fixed**
- **GPU went undetected on hosts where nvidia isn't Docker's default runtime** (stock Ubuntu/Debian/Mint, where GPU containers normally opt in per-container with `--gpus all`). The monitor exposed the card only through the `NVIDIA_*` env vars, which the toolkit honours only for the default runtime — so the card worked everywhere else but the dashboard reported "no GPU detected" (#203). Added a **`docker-compose.gpu.yml` override** that requests the nvidia runtime for just this container (no global-default change, still starts on GPU-less hosts), and corrected the Setup-tab remedy + compose comments to include the **`--force-recreate`** step a plain restart was silently skipping.
- **AMD / Intel GPUs now show in the per-host tab** instead of a misleading "no NVIDIA GPU" empty state, and nameless / NVIDIA-without-`smi` cards are handled gracefully. (#206)
- **Overview engine gauges** keep the number + `%` centred in the ring at every size. (#211)

## [0.21.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.21.0) — 2026-06-23 · **See your power bill by the hour — and which GPU is burning it**
*Three new questions answered at a glance: when is your lab most expensive, which GPU is doing the work (whoever made it), and is anything down?*

**Added**
- **Busy-hours cost heatmap on the Costs tab.** A 7×24 grid — local day-of-week × hour — of your lab's typical total draw, **shaded by cost per hour** once a tariff is set (by power otherwise). It turns months of history into one picture of *when* your rig actually costs you money, with the busiest and quietest slots called out and a busy-vs-quiet spread. No setup; it fills in after about a day of samples.
- **Any-vendor GPU support — the panel is no longer NVIDIA-only.** **AMD** GPUs are read on Linux straight from the kernel's `amdgpu` interface — **no ROCm, no vendor tools** — and **AMD and Intel** GPUs (including integrated) are read on Windows hosts, so the card finally shows up with its name, utilisation and VRAM. Strictly additive: NVIDIA and GPU-less hosts behave exactly as before.
- **Built-in uptime monitoring.** Watch any **HTTP endpoint or TCP port** from the same container — know the moment anything stops responding, with a heartbeat strip, **24h/7d uptime %**, latency, and **smart per-check alerts** (anti-flap confirm, recovery quoting the downtime, optional slow-response warning). Nothing else to self-host.

**Polish**
- The busy-hours heatmap uses the dashboard's SVG icon family and a fixed-width day column; accessibility and layout passes throughout.

_Rolls up the work shipped through `next` since 0.17.3 into one release. Still pure Python + Flask — no new runtime dependencies._

## [0.20.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.20.1) — 2026-06-23 · _patch_
_Silent patch — polish on the new busy-hours heatmap._

**Fixed**
- **Heatmap icons** now use the dashboard's SVG icon family — a calendar glyph in the heading and trending-up / trending-down / balance icons on the callouts — instead of raw emoji, so the card matches every other panel.
- **Heatmap layout** — the day-of-week label column is pinned to a fixed width (`table-layout:fixed`); on wide screens it could previously stretch to roughly half the card and squash the 24 hour cells.

## [0.20.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.20.0) — 2026-06-23 · **Busy hours — see when your lab burns power**
*A 7×24 day-of-week × hour heatmap on the Costs tab shows the rhythm of your lab at a glance — when it's busy, when it's idle, and (with a tariff set) when it's expensive.*

**Added**
- **Busy-hours heatmap on the Costs tab.** Every history sample is bucketed by its **local weekday and hour** and averaged, giving a 7×24 grid of typical total draw (GPU + CPU + DRAM). Colour scales by **cost-per-hour** once you've set a tariff, or by **power** otherwise — so it's useful even before you add a price. Callouts surface the **busiest** and **quietest** slots and, when priced, the spread between your busiest and quietest quarter of hours. Served from a new pure-Python `/api/cost/heatmap` endpoint (own 30-day window, aggregated outside the DB lock), reusing the same tariff machinery as the rest of the Costs page so the €/kWh maths never diverges. The grid is a real `<table>` with per-cell `aria-label`s, sparse cells are dimmed by sample count (honest about coverage), and it fills in after about a day of history.

## [0.19.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.19.0) — 2026-06-23 · **AMD & Intel GPUs join the party**
*The GPU panel is no longer NVIDIA-only — AMD and Intel cards now show up too, with no vendor tools to install.*

**Added**
- **AMD GPU support on Linux — no ROCm required.** Both the hub's own collector and every monitored Linux host now read AMD cards through the in-kernel `amdgpu` sysfs interface (`gpu_busy_percent`, `mem_info_vram_total`/`used`, and hwmon temperature/power) — so an AMD box shows the GPU panel (name, utilisation, VRAM, temp, power) with zero configuration. (#1)
- **AMD & Intel GPU support on Windows hosts.** The Windows probe now falls back to Windows' built-in GPU performance counters + WMI when `nvidia-smi` is absent, surfacing AMD and Intel GPUs — including **integrated** graphics — with name, utilisation and VRAM.

**Changed**
- The GPU back-end is now vendor-aware: NVIDIA continues through `nvidia-smi`, and the AMD/Intel paths are consulted **only** when `nvidia-smi` reports nothing — so NVIDIA and GPU-less hosts behave exactly as before. Per-card clock/throttle enrichment and per-process VRAM attribution remain NVIDIA-only for now (AMD per-process attribution is tracked as a follow-up).

## [0.18.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.18.0) — 2026-06-22 · **Is my lab up? — Uptime monitoring built in**
*Point it at any URL or port and it watches them for you — heartbeat, uptime %, and alerts that page once (not on every dropped packet) and tell you when it's back.*

**Added**
- **Uptime checks — built right in.** A new **Uptime** tab monitors any **HTTP endpoint or TCP port** (your own services, a NAS, a remote site) from inside the container — a heartbeat strip, **24h + 7d uptime %**, latency and last error per check. Probes run on a dedicated worker (never the metrics sampler), each bounded by its own timeout, so a hanging endpoint can't stall the rest; nothing is probed until you add a check.
- **Smart per-check alerting**, reusing your existing channels (Discord / ntfy / Telegram): a check is only called **DOWN after N consecutive failures** (anti-flap — a single dropped packet won't page you), a **recovery alert quotes the downtime**, and an optional **latency warning** fires when an endpoint is up but slow. On by default per check; honours the global minimum severity.
- **Overview at a glance** — a 🛰 uptime chip on the cockpit's fleet rollup (red the moment a check is down), and down/slow endpoints surface in the Insight Feed.

**Privacy:** check targets/labels/errors stay on the private dashboard + authed API — they never reach the public status payload.

_Rolls up the silent 0.17.1–0.17.3 patches below._

## [0.17.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.3) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Changed**
- **Overview rebuilt as a 3-column mission-control cockpit.** The fleet rail (every host at a glance), the engine + cost column, and the live insight feed + cost leaderboard sit side by side, with a containers / services / diagnostics strip beneath — a clear glance → scan → drill hierarchy instead of the old stacked layout.
- **The engine shows three gauges — GPU · CPU · RAM — for the focused host**, with a **This host / Whole fleet** toggle that aggregates the whole homelab (average util, summed VRAM / cores / RAM). Each ring turns amber → red as it saturates.
- **Calmer, more deliberate styling** across the Overview: solid panels with soft depth, neutral ink for figures, semantic colour reserved for state, restrained motion.

**Fixed**
- **Accessibility** — every tab now passes a WCAG 2.1 AA contrast scan in both themes (muted captions, warn/ok text on tinted surfaces, in-text link distinction, keyboard-focusable scroll regions, full reduced-motion coverage).
- **Mobile** — fixed a horizontal-overflow bug (the chart canvas could pin the page wider than the viewport); the dashboard is now fully responsive down to 320px, and the GitHub / Discord / social cluster is shown again on small screens.
- Removed the duplicated detailed-fleet table from the Overview (the fleet rail is the canonical view).

## [0.17.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.2) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Fixed**
- **Alerts now name the machine.** Every notification — Discord, ntfy and Telegram — is prefixed with `[<machine>]`, so when you monitor many hosts you can tell at a glance *which* one a "Container unhealthy" / "Disk at 95%" / "GPU VRAM pressure" alert is about. The **Test** button shows the same label so you can confirm it. No configuration needed — the name comes from the host's reported hostname.

## [0.17.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.1) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Fixed**
- **Discord notifications no longer fail with 403.** Discord's webhook API sits behind Cloudflare, which rejects the default `Python-urllib` agent (error 1010). Outbound notifier POSTs now send a real `User-Agent`, so the alert-settings **Test** button works.
- **Fleet "online/offline" no longer flaps.** The overview summary occasionally showed healthy hosts as offline, then online again next refresh. Hosts are now polled **concurrently** (one slow remote can't age the others out) and the online flag uses a generous, timeout-aware staleness window — a single slow or missed poll cycle can't flip a host offline.

**Changed**
- **Overview tab reordered** — fleet data table first, then the AI/GPU workload band, then MCP, with **Setup & requirements** moved to the bottom.
- **CI/CD**: the Discord release announcer now skips patch releases, matching the selfh.st gate — both speak only on minor+ versions.

## [0.17.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.0) — 2026-06-18 · **Ask Your Homelab — over MCP**
*Costs and experiments are now MCP tools, so Claude (or any AI) can tell you what last night cost. Bonus: it speaks Chinese now, and updates itself.*

> 0.16 turned HomeLab Monitor into the **AI Lab Cockpit** — GPU, models and the power bill on one page. 0.17 makes that cockpit **conversational and personal**: your AI can now read the costs and the experiment runs straight over MCP, the whole UI can speak another language, and the dashboard can upgrade itself in one click. Still pure Python + Flask, no new dependencies.

**Ask your AI what it costs (MCP)**
- The built-in **read-only MCP server** gains four tools so an agent can finally see the money side of the lab: `get_costs` (machine draw + cost, with a ranked per-process / container / service / model breakdown), `get_entity_cost` (drill into one line item), and `get_experiments` / `get_experiment` (tracked runs priced by the **real GPU energy they burned** — loss curve and watts on the same timeline). **16 tools total**, still **nothing mutated**. Ask *"what did my homelab cost last night, and which model is the most expensive thing on the GPU?"* and let it pick the tools.

**Make it yours — in your language**
- A full **internationalisation framework**: every tab, modal, toast and helper string is translatable, served from `/locales` and shipped in the image. **Simplified Chinese (zh-CN)** is the first complete translation (96% coverage). Closes #148.

**A dashboard that maintains itself**
- **One-click self-update** from the dashboard — opt-in, **off by default** (`ALLOW_SELF_UPDATE`), confirmation-gated, and the monitor's *first and only* write action. It is **not** exposed over MCP. Closes #142.
- **Toast notifications** — every action confirms itself with a quick, dismissible toast instead of a silent state change. Closes #139.

**Brand & community**
- **New logo — radar, not satellite.** A designed radar mark is now *the* brand everywhere — sidebar, header, favicon, README and the docs site — in a transparent variant for the dark UI and a tiled app-icon for the favicon. Radar fits what the tool does: sweep the fleet, surface the blips.
- **Community** — a "Join the Discord" button and a permanent invite in the sidebar, a "Buy me a coffee" support button, and a live Docker-pulls badge in the README. Come help shape the roadmap: <https://discord.gg/tpKWKEdSQN>

**Fixed**
- Network › Throughput legend filter no longer resets on every auto-refresh (#155).
- Clearer **Topology / Machine** card labels on System › Hardware (#154).
- The offline "What's new" changelog now renders italics, blockquotes and the bold subtitle (#173).
- Bulgaria (BG) tariffs switched from BGN to EUR (#145).
- The brand logo no longer 404s in the container — the Dockerfile copies the whole `static/` dir instead of listing files one by one.

The unfinished personalisation items from the "Make it yours" epic (#147) — reorderable tabs (#33), the AI Models Hall of Fame (#23) and per-service notification rules (#24) — carry forward to the next cycle.

**Thanks to**
This release leaned hard on the community — it wouldn't be what it is without them.
- **@pehota** — one-click self-update (off by default), a socket-safety fix, and migrated Bulgaria's tariffs to EUR
- **@krishnapandey1504** — built the unified toast notification system
- **@DevCrox** — wired the dashboard's status messages onto the new toast system
- **@laishettikarthik-tech** — fixed the network throughput legend filter resetting on refresh
- **@koteshyelamati** — clearer Topology / Machine card labels

Five external contributors on one release. 🙏

## [0.16.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.16.0) — 2026-06-14 · **The AI Lab Cockpit**
*Your GPU, your models, and your power bill — finally on the same page.*

> Two milestones land together here: the **deeper-visibility** work, and the big push that turns HomeLab Monitor into the at-a-glance dashboard for anyone training models or serving LLMs at home. Still pure Python + Flask, no new dependencies — it just reads `nvidia-smi`, `/proc`, `/sys`, the Docker socket and your model servers directly.

**See what your GPU is actually doing**
- **GPU truth** — your card says 100% util, but is it throttling, memory-bandwidth-bound, or quietly drooping its clocks? The GPU tab now decodes `nvidia-smi`'s throttle reasons (a red banner the moment it's power-capped or too hot), and adds memory-bandwidth util, core/mem clocks, power-vs-limit headroom and performance state — all from the same call.
- **Multi-GPU** — every card gets its own live panel; the pooled views still sum across them. Closes #95.
- **Model intelligence** — Ollama models show their parameter size, quantisation and context length at a glance, and vLLM/TGI servers get a live serving strip: **tokens/sec**, requests running/queued, KV-cache fill and TTFT, read straight from their `/metrics`.

**Know what it costs**
- **Costs** gets its own page — power becomes money, per machine, then **per component** (GPU measured via `nvidia-smi`, CPU/DRAM via RAPL), then **per process, container or model** you can click to drill into, over any timeframe. Day & night tariffs (UK Economy 7, France Heures Creuses, …) — or just pick your country for a sensible estimate. Honest by design: every watt is measured or a baseline you set, never a guessed wall figure. Closes #25.

**Track your runs**
- **Experiments** — push a run from Jupyter, Colab or Kaggle with a tiny one-file client (or mirror it from **MLflow**), and it comes back **priced with the real GPU energy it burned** — loss curve and power draw on the same timeline. The API keys are yours to create, name, expire and revoke.
- **Notebooks & tools** — auto-discovers Jupyter, TensorBoard, MLflow, W&B, Streamlit and Ray, and flags the idle notebook squatting on your VRAM.

**The rest of the lab, deeper**
- **Network I/O** — host throughput and per-container top talkers. Closes #30.
- **Container logs** — click a container, tail its logs in a side drawer. Closes #28.
- **Top processes** — a mini-htop on the System tab: who's eating CPU and RAM, by command. Closes #32.
- **Adaptive host timeouts** — a slow box that timed out on poll now learns its own sweet-spot instead of being marked down. Closes #99.
- Click the **logo** to jump back to Overview. Closes #98 — thanks @DevCrox for the PR.

**Tidied up**
- A dedicated **AI** zone in the sidebar, so a homelab admin who doesn't care about models isn't put off by "Experiments". The old Alerts tab is now one **Settings** home with **Alerts / Costs / Integrations** sub-tabs.
- A proper visual pass: a real SVG icon set in place of emoji, consistent type/spacing/radius tokens, visible keyboard focus everywhere, and a light theme that finally behaves.

130+ unit tests, shipped one PR at a time through CI → `:next`. Research and design notes live under `design/ai-cockpit/`.

## [0.15.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.15.0) — 2026-06-12
*Polish, safety & sharing.*
- **Snapshot & Share** — grab the current tab as a PNG, or share a generic link + blurb (no host data) to X, Reddit, Hacker News, LinkedIn, Mastodon, Bluesky, Telegram, WhatsApp and more. Closes #31, #87.
- **Service ports** — the Services tab shows each service's listening port as a click-through link, now including forking services (Pi-hole FTL, dnsmasq, libvirtd) whose socket lives in a child process — resolved via the unit's cgroup. Closes #83. Thanks @ravvdevv for the PR, and @vaishnavidesai09 for the scoping.
- **Backup & restore** — one-click SQLite export/import so you never lose your history. Closes #86. Thanks @mohd-ibadullah.
- **MCP status pill** — see at a glance when an AI agent is connected to the built-in MCP server. Closes #84. Thanks @mohd-ibadullah.
- **Telegram alerting** — a third notification channel alongside Discord and ntfy. Closes #27. Thanks @mohd-ibadullah.
- **Desktop polish** — the dashboard fills the width and tints nav vs. main for easier scanning. Closes #85. Thanks @vikasvardhanv.
- **What's new** — a one-time welcome modal after an upgrade, rolling up the changelog for every release since the one you last ran (served offline from this file).

Huge thanks to @mohd-ibadullah, @vikasvardhanv and @ravvdevv for the PRs this cycle — and @vaishnavidesai09 for the scoping on #83.

## [0.14.4](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.4) — 2026-06-10
- **Fix:** Services tab now refreshes immediately when you switch hosts. The host-switch path in `refreshCurrentTab()` was missing a `services` branch, so Services only re-rendered on tab-change instead of right away. Closes #82. Thanks @mohd-ibadullah for the first-time contribution.

## [0.14.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.3) — 2026-06-09
- **Fix:** the built-in MCP server over HTTP rejected remote clients with **421
  Misdirected Request**. The MCP SDK's DNS-rebinding guard only trusts a
  `localhost` Host header, so the documented `claude mcp add --transport http
  homelab http://YOUR-HUB:9810/mcp` failed whenever the hub was reached by name or
  IP. The HTTP/SSE transport now disables that localhost-only check by default
  (safe — every tool is read-only), and a new `MCP_ALLOWED_HOSTS` (plus optional
  `MCP_ALLOWED_ORIGINS`) env lets you lock it back down to specific hosts. stdio
  is unaffected.

## [0.14.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.2) — 2026-06-09
- **Fix:** correct the `io.modelcontextprotocol.server.name` image label to match the
  publisher namespace casing (`io.github.SikamikanikoBG/homelab-monitor`) so the
  image passes the official MCP Registry's OCI ownership check. `server.json` trimmed
  to a ≤100-char description and the placeholder remote dropped (OCI/stdio entry is
  the canonical one for a self-hosted server). Discoverability only — no runtime changes.

## [0.14.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.1) — 2026-06-09
- **MCP registry packaging** — the image is now publishable to the official
  [MCP Registry](https://registry.modelcontextprotocol.io): added the
  `io.modelcontextprotocol.server.name` image label and a root `server.json`
  (OCI + streamable-HTTP entries). Also added a `smithery.yaml` so the server can
  be catalogued on [Smithery](https://smithery.ai), and a 400×400 logo. No runtime
  changes — discoverability only.

## [0.14.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.0) — 2026-06-09
- **Built-in MCP server** — a first-class [Model Context Protocol](https://modelcontextprotocol.io)
  server now ships **inside the same image**, so Claude (or any MCP client) can
  connect to the monitor and explore the whole homelab as named, **read-only**
  tools — no extra container. Served on `MCP_PORT` (default **9810**) alongside the
  dashboard; connect with `claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp`.
  Full dashboard parity: `list_hosts`, `get_host`, `get_snapshot`, `get_containers`,
  `get_services`, `get_memory` (per-service/per-process RAM), `get_gpu` (util/VRAM/
  power + caller attribution), `get_ai_models`, `get_history` (charted series),
  `get_events`/`get_alerts`, and `scan_disk` (WizTree-style folder treemap) — plus
  `metrics`, `health` and `changelog` resources. Read-only by design: no write tools.
  Opt out with `ENABLE_MCP=0`. Docs:
  [MCP server](https://sikamikanikobg.github.io/homelab-monitor/mcp/). Closes #70.

## [0.13.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.13.1) — 2026-06-07
- **Mobile-friendly dashboard** — the dense tables (Containers, Services, AI
  Models, Network) no longer push the whole page sideways on a phone; each one
  now scrolls within its card. Verified no horizontal overflow down to 360px.
- **Smarter OS-upgrade hint** — a host is now offered the **next reachable**
  release instead of the newest one, so e.g. an Ubuntu 22.04 LTS box is pointed
  at 24.04 rather than a newer LTS it can't `do-release-upgrade` to directly.
- **Fix:** an empty amber banner could show on every tab even when setup was
  healthy (the diagnostics banner's `display` overrode its hidden state).
- Demo video now embedded from **YouTube** on the README and landing page.

## [0.13.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.13.0) — 2026-06-06
- **Windows host monitoring** — register and monitor a **Windows** machine over
  SSH, agentless, via a built-in **PowerShell probe** (no install). Same fleet
  row + System / Network / Services tabs; per-service RAM; GPU via `nvidia-smi`.
- **Hosts onboarding redesigned** — clear three-step flow, theme-safe in light &
  dark, with a **per-OS command chooser** (Linux / Windows user / Windows admin).
- **Containers: RAM vs VRAM** in separate columns, with RAM as the real resident
  set (page cache excluded) and a **table total**.
- **Memory map** (System tab) — interactive treemap of RAM by **container** and
  **systemd service**; works on Docker-less hosts too.
- **New Disks tab** — **WizTree-style** nested folder treemaps; scan a disk,
  drill into folders. On-demand and cached.
- UI refresh — prominent GitHub star/repo/issue cluster, cohesive version &
  update chrome, faster new-release checks.

## [0.12.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.12.0) — 2026-06-02
- **Container disk that's actually right** — the Containers tab now counts each
  container's true footprint (writable layer **plus** its volumes & bind mounts;
  mounts shared between containers are excluded), so heavy containers like Ollama
  and Immich show their real GB instead of a near-empty writable layer.
- **AI Models** now recognises **WhisperX / whisper-asr-webservice** and a dozen
  more servers (SGLang, Triton, Wyoming voice, OpenLLM, LiteLLM, GPUStack,
  Cortex/Jan, …).
- Time-range picker now shows on **every** tab.
- Sidebar brand no longer truncates.

## [0.11.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.11.0) — 2026-05-31
- **AI Models: detect-all + caller attribution** — every recognised model server
  is listed (and stays listed as **Idle** when its model unloads), with a
  **"Driven by"** breakdown showing which services are calling each server.

## [0.10.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.10.0) — 2026-05-31
- **System / Network / Security tabs** — host inventory (OS, kernel, arch, init
  system, hardware), per-host network interfaces & listening sockets, and a
  read-only security posture check (firewall, SSH hardening, SELinux/AppArmor,
  fail2ban, reboot-pending, auto-updates).

## [0.9.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.1) — 2026-05-31
- Accurate remote CPU temperatures.

## [0.9.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.0) — 2026-05-30
- **Container & Service vital signs** — per-container and per-systemd-unit health,
  uptime, memory and ports.

## [0.8.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.8.0) — 2026-05-30
- **Multi-machine monitoring** — register other boxes over SSH from the Hosts tab
  and see every host's vitals side-by-side. No agents, just SSH + Python 3.

## [0.7.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.7.0) — 2026-05-29
- **Self-healthcheck** — `/healthz` liveness endpoint + Docker `HEALTHCHECK`.

## [0.6.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.3) — 2026-05-29
- Rendered release notes in the in-app update modal.

## [0.6.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.2) — 2026-05-29
- Faster recovery from negative update-check results.

## [0.6.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.1) — 2026-05-29
- Branded GitHub star button + contribution nudge.

## [0.6.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.0) — 2026-05-29
- In-UI update notification + favicon.

## [0.5.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.5.0) — 2026-05-28
- **Alerting (Discord + ntfy.sh)** — edge-triggered push notifications, configured
  entirely from the Alerts tab.

## [0.4.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.1) — 2026-05-26
- Accurate Prometheus label series.

## [0.4.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.0) — 2026-05-26
- **Prometheus `/metrics` endpoint** — standard scrape endpoint reading from the
  in-memory snapshot (no extra polling).

## [0.2.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.2.0) — 2026-05-25
- Local-AI model drill-down, contention intelligence & host health.

---

_Older tags exist in the repository history. For the authoritative, complete
notes of any release see the
[GitHub releases page](https://github.com/SikamikanikoBG/homelab-monitor/releases)._
