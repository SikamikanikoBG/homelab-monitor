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

## Branch off the latest `main`

Before you start, please **sync `main` and branch from it**:

```bash
git checkout main
git pull origin main
git checkout -b your-feature
```

`main` moves, and rebasing a stale branch onto a moved-on `main` is the most
common source of merge churn here. Fresh branches save everyone time.

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

## Style

- Keep it **plug-and-play** — anything new should work with `docker compose up
  -d --build` and no extra config. If a feature needs config, give it a safe
  default and make it optional.
- Keep the tone **humble and welcoming** in UI copy, README, and commit
  messages. This project is for hobbyists; please don't make anyone feel small
  for not knowing something.
- Match the existing code style — short functions, plain names, comments only
  where the *why* isn't obvious from the code.

## Submitting a PR

- Open an issue first for anything larger than a small fix, so we can agree on
  the shape before you spend time on it.
- Keep PRs focused — one feature or fix per PR is easier to review.
- Leave the version bump to the maintainer; it happens on release, not per PR.

Thanks again — every issue, suggestion, and PR helps. 🛰️
