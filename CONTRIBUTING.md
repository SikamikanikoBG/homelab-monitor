# Contributing to HomeLab Monitor

Thanks for thinking about contributing! This is a small hobby tool meant to help
fellow home-labbers, so the bar for contributing is intentionally low — a
typo-fix PR is just as welcome as a new GPU back-end.

## Run it locally

The repo is a single Docker Compose service. You don't need Python on your host.

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Open **http://localhost:9800** (or `http://<your-host-ip>:9800` from another
device on your LAN/VPN).

History lives in `./data/gpu.db` — a bind mount that survives rebuilds. Delete
the file if you want a clean slate.

To rebuild after a code change: `docker compose up -d --build` again. To follow
logs: `docker compose logs -f`.

> **GPU panels** need an NVIDIA GPU and the
> [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
> Without a GPU the container, service and host panels still work fine — handy
> for developing non-GPU features on a laptop.

## Where to branch from — and where to send the PR

This repo runs two long-lived branches:

- **`main`** — the stable, released line. It's what `docker compose pull` ships
  and what people clone to *run* the tool. Releases are cut from here as `vX.Y.Z`
  tags. **Don't send feature PRs here.**
- **`next`** — the integration branch for the upcoming version. **All feature and
  fix PRs target `next`.** When `next` is ready, the maintainer merges it into
  `main` and tags a release.

So before you start, **sync `next` and branch from it**:

```bash
git fetch origin
git checkout next
git pull origin next
git checkout -b your-feature
```

Then open your PR **against `next`** (GitHub defaults the base to `main` — change
the base branch dropdown to `next`).

`next` moves, and rebasing a stale branch onto a moved-on `next` is the most
common source of merge churn here. Fresh branches save everyone time.

> Tiny, release-worthy hotfixes (a typo in shipped docs, a crash in the current
> release) may target `main` directly — when in doubt, target `next` and say so
> in the PR.

## The "add a monitor" pattern

The codebase is deliberately small and modular so adding a new subsystem is a
short, predictable change. Follow the existing pattern:

1. **Backend collector.** In `app.py`, add a `collect_<thing>()` that returns:
   ```python
   {"available": bool, "summary": {...}, "items": [...]}
   ```
   Return `{"available": False, ...}` (rather than raising) when the subsystem
   isn't present — the dashboard should degrade gracefully, not error.

2. **Wire it into the scan.** Call `collect_<thing>()` from `health_scan()` so
   the background thread keeps it fresh, and include it in the `/api/health`
   response.

3. **Frontend tab.** In `static/dashboard.html`, add:
   - one entry to the `TABS` array,
   - one `<section data-tab="...">` for the tab body,
   - a small renderer that reads from `/api/health`.

   No build step, no framework — vanilla JS and the vendored Chart.js.

That's the whole pattern. The same shape applies to "add a model-server probe"
(append to `PROBES` in `app.py`) and "add an alert channel" (extend the alert
dispatcher).

## Extending the multi-host probe

Since 0.8, the hub can monitor *other* boxes too. To extend what's collected
remotely:

1. **Add a reader to `probe.py`.** Returns a small dict that gets merged into
   the JSON blob the hub caches per host. Keep it pure stdlib (no install on
   the remote) and short-timeout (≤3s) so a stuck command never blocks the
   poll cycle. Example:
   ```python
   def read_smart():
       try:
           r = subprocess.run(["smartctl", "-A", "/dev/sda", "-j"],
                              capture_output=True, timeout=2)
           ...
       except Exception:
           return {}
   ```
2. **Wire it into `host_poller`** automatically — `_probe_host_metrics` already
   ships whatever `probe.py` prints, so adding a new key is enough.
3. **Render it.** Either add it to the All-hosts table (`/api/fleet` reads
   `host.<your_key>`) or to a per-host tab.

Keep `probe.py` self-contained — the hub pipes it over SSH on every cycle, so
external deps would defeat the "agentless" promise.

## Style

- Keep it **plug-and-play** — anything new should work with `docker compose up
  -d --build` and no extra config. If a feature needs config, give it a safe
  default and make it optional.
- Keep the tone **humble and welcoming** in UI copy, README, and commit
  messages. This project is for hobbyists; please don't make anyone feel small
  for not knowing something.
- Match the existing code style — short functions, plain names, comments only
  where the *why* isn't obvious from the code.
- Adding an animation? See [`docs/animations.md`](docs/animations.md) — CSS
  transitions/`@keyframes` are covered automatically by the reduced-motion
  kill switch; JS-driven number tweens must go through `mcCountUp()`, not a
  new `requestAnimationFrame` loop.

## Submitting a PR

- **Target `next`**, not `main` (see "Where to branch from" above).
- Open an issue first for anything larger than a small fix, so we can agree on
  the shape before you spend time on it.
- Keep PRs focused — one feature or fix per PR is easier to review.
- CI runs a build + boot smoke on every PR; please get it green before asking
  for review.
- Leave the version bump to the maintainer; it happens on release, not per PR.

Thanks again — every issue, suggestion, and PR helps. 📡
