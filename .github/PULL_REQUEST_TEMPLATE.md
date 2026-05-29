<!-- Thanks for sending a PR! A short description and a ticked checklist are all
we need — the rest is optional. -->

## What this changes

<!-- One or two sentences. Link related issues with "Fixes #123" if any. -->

## Checklist

- [ ] Branched off the **latest `main`** (`git pull origin main` before
      branching) — avoids stale-branch merge churn.
- [ ] Tested locally with `docker compose up -d --build` and the dashboard
      behaves as expected.
- [ ] No version bump in this PR — version bumps are handled by the maintainer
      at release time.
- [ ] For a new monitor / probe: followed the **"add a monitor" pattern** in
      `CONTRIBUTING.md` (collector → `health_scan()` → `/api/health` → `TABS`
      entry + section + renderer). *(Skip if not applicable.)*

## Notes for the reviewer

<!-- Anything worth flagging: trade-offs you considered, screenshots of UI
changes, things you're unsure about. -->
