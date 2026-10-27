---
title: AI agent — chat with your lab, automate the chores
description: An optional in-app AI agent for HomeLab Monitor. Ask in plain words, act behind a confirm card, run scheduled automations from templates — with a per-host policy and a full audit trail. Works with local models (Ollama, vLLM, llama.cpp, LM Studio) or your own cloud key.
---

# AI agent

HomeLab Monitor can host an **AI agent** that knows your lab. It has two halves:

- **Ask** — chat with your fleet from the **Agent** tab or the floating **Ask** button on every page.
  Answers come from the same read tools the [MCP server](mcp.md) exposes, so they are grounded in
  what the dashboard actually sees. Where you allow it, the agent can also act — behind a confirm
  card.
- **Automate** — switch on ready-made templates (Docker cleanup, model housekeeping, disk-space
  watch, system maintenance, GPU health, update and cost reports, container health, backup
  freshness, data & log cleanup) or write your own from a prompt. They run on a schedule and send
  you a report.

**It ships switched off.** If you never turn it on, nothing changes: no new routes answer, no
threads start, no tables are created and the dashboard looks exactly as it did.

## Turning it on

Set `AGENT_ENABLED: "1"` in `docker-compose.yml` and recreate the container. The **Overview** then
shows **Your lab can answer you now** with three steps and a **Set it up** button (about a minute):

1. **Secure** — set the dashboard password (with a strength meter; forgot it later? see
   [Security](security.md#dashboard-login)). The agent can act on your machines, so it only runs
   behind a login; you stay logged in in this browser.
2. **Model** — the model servers the hub already knows about are found, added and tested while
   you watch. Each model says **Works with the agent** or **Can't be used**; the recommended one
   (calls tools, local first, the largest context) is picked for you. A cloud model with your own
   key, or a server by its address, are one click away.
3. **Permissions** — one preset for every machine, and for machines you add later:
   **Look only** (all Passive), **Ask me first** (Active, every action asks — recommended) or
   **Handle the small stuff** (low-risk actions that need no root run by themselves, within the
   unattended rules below; the rest asks). Optionally switch on a starter routine (container
   health, Docker cleanup, disk space watch) with sensible defaults.

**Finish and ask something** switches the agent on and opens the chat with suggestions. The setup
resumes where you left it; **Not now** folds the invitation to one line (remembered for your login,
or in this browser before there is one). Everything the setup does is also an API call
(`/api/agent/setup`, `/api/agent/setup/models`, `/api/agent/policy/preset`, `/api/agent/setup/finish`)
and the server checks each step. **Agent → Settings → Run the setup again** reopens it.

Once it is set up, the Overview shows **Ask your lab**: a question box, suggestions, the model in
use, approvals **waiting for you**, and one line per routine run lately. It is hidden on a wall
display (kiosk mode).

## Picking a model

The setup does this for you; **Agent → Settings → Models & keys** is where to change it. Local model servers the hub already knows about (Ollama, vLLM, llama.cpp, LM Studio, LocalAI, and
any custom AI server from Settings → General) are listed under **Found on your lab**. Click
**Add**, then **Test** on a model:

- **tool calls: works** — the model called a test tool correctly. Required.
- **multi-step: yes** — it also read a tool result and followed up. Recommended for automations;
  small models (7–8B) often manage questions but get weak on multi-step tasks.
- **no tool calls** — it answers in text only, so the agent refuses to use it (it would make
  things up instead of looking).

Cloud providers work with **your own key**: OpenAI, Anthropic, Google Gemini (experimental),
Azure OpenAI, Groq, Mistral, DeepSeek, xAI, OpenRouter, Together, or any OpenAI-compatible URL.
Keys are **write-only** — stored on the hub, never sent back to the browser, left out of every
backup. Better still, use `env:NAME` or `file:/run/secrets/name` as the key so it never enters the
database. Set a price per million tokens on a cloud model to see what each run cost and to enforce
the daily spend cap.

AWS Bedrock isn't built in yet; reach it through an OpenAI-compatible gateway (OpenRouter, LiteLLM).

## Who may do what — the policy

Every host has a mode:

| Mode | Meaning |
|---|---|
| **Off** | The agent can't even see the host. |
| **Passive** | Read only. **New hosts start here.** |
| **Active** | May act, within the actions you allow below. |

Every action is **Deny / Ask / Allow** per host, and **Deny until you change it**. Actions come
from a fixed catalogue — there is no free-form shell, ever:

| Action | Risk | Runs unattended? |
|---|---|---|
| Start / restart a container, change restart policy | low | yes, if Allowed (max 3× a day per target) |
| Stop a container | medium | only if you unlock it |
| Start / restart a service | medium, needs root | never (always asks) |
| Remove dangling images, remove build cache | low | yes, if Allowed |
| Remove stopped containers older than N h | medium | only if you unlock it |
| Shrink the systemd journal | medium, needs root | never |
| Unload an Ollama model | low | yes, if Allowed |
| Delete an Ollama model | high | never — and refused while it is loaded |

Read-only checks (Docker disk use, journal size, pending updates, reboot needed, kernel GPU errors,
old files under a watched path) work on any host that isn't Off.

**Not in this version:** reboot, volume removal, deleting files.

Rules the server enforces no matter what the model says:

- `ENABLE_CONTROLS=0` turns every action into Deny.
- **Protected targets** are never touched: the monitor's own container, whatever serves the model
  in use, the service the hub's SSH goes through, and anything you mark under Agent policy.
- **Unattended runs can only be stricter.** Medium-risk actions wait for approval unless you unlock
  them per host; high-risk and root actions always wait.
- The policy is checked when the model proposes an action **and again right before it runs**
  (after an approval too).

**Windows hosts:** container actions run `docker` through PowerShell and
service actions use `Start-Service` / `Restart-Service`; there is no sudo, so
whether they work depends on which `authorized_keys` file holds the hub's key
(a plain user's, or `administrators_authorized_keys`). A Windows command gets 30
seconds; a slow `Restart-Service` can take longer and be reported as failed
even though the service restarted — check the Services tab before retrying.
Journal and package checks aren't offered for Windows.

Root actions run as `sudo -n`. If the SSH user can't do that, the agent shows the exact sudoers line
that would allow that one command (no wildcards). Note that a user in the `docker` group is
already root-equivalent on that box.

## Chat and confirm cards

Automations only take the actions ticked under **Actions** in their New/Edit
dialog (templates come with theirs ticked); none ticked means it only reads and
reports.

Ask in plain words: *"why is my server slow?"*, *"what's using the GPU?"*, *"restart web on lab1"*.
Each tool the agent used shows as a chip you can open. When an action needs your OK you get a
card with the exact action, host, risk and command; **nothing runs until you approve**. Reject
tells the agent no, and it won't try another way. A refused action names the setting that would
allow it.

The floating **Ask** button knows which page and host you're looking at. It is hidden in display
mode (`/?display=1`).

## Automations

**Agent → Automations → Templates → Activate**: pick hosts (only those the template fits), a
schedule (or a cron line, with the next runs previewed in your time zone), a model, where the report
goes (in-app, or your Discord/ntfy/Telegram/email/Slack/webhook alert channels) and a mode:

- **Report only** — looks and reports, no actions.
- **Propose** — every action waits for your approval.
- **Act within policy** — low-risk Allowed actions run; the rest wait for approval.

**Run now** runs it once; **Dry run** runs it in Report mode. Each slot fires once, even across
restarts; slots missed while the hub was down are skipped (or the latest one runs once, per
task). A busy GPU defers a run on a local model up to 30 minutes, then skips it. Cloud models run at
most hourly and stop at the daily spend cap. **Pause all automations** stops everything scheduled.

Proposals wait under **Approvals** (and as a badge on the Agent tab) for 24 hours; the same proposal
from several nights collapses into one card.

## Activity and audit

**Agent → Activity** lists every run — chat and scheduled — with its model, tokens, cost and outcome.
Open one for the timeline: each tool call, each policy decision and why, who approved, and what
happened. Export as JSON or CSV.

The step log is append-only (database triggers). Every action is also written as one JSON line to
the container's output, so `docker logs homelab-monitor | grep agent_audit` keeps a copy outside the
database. Old runs follow `RETENTION_DAYS`.

## What goes to a cloud provider

With a cloud model, your question and the tool results the agent reads (host names, container
names, metrics, log excerpts it asked for) are sent to that provider. Use a local model to keep
everything on your network.

## Settings reference

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_ENABLED` | — | `1` makes the agent available. Unset: every `/api/agent/*` route is a 404. |
| `AGENT_MAX_RUNS` | `2` | Runs at once; the rest queue (chat goes first). |
| `ADMIN_PASSWORD` / `ADMIN_PASSWORD_FILE` | — | Set the dashboard password at boot, only if none is set. |
| `ENABLE_HOST_RUN` | `0` | The old `/api/hosts/<name>/run` arbitrary-command route; off by default. The agent never uses it. |

Agent settings (time zone, budgets, spend caps, approval expiry, GPU courtesy thresholds) live in
the Agent tab and in `GET/PUT /api/agent/settings`.

**Budgets always end with an answer.** A chat run gets 12 model calls, 300 s and 120,000 tokens
by default; an automation 25 calls, 600 s and 200,000 tokens. The token budget counts what the
model really reads and writes: the first prompt, then only what each call adds (new tool
results and replies), not the whole conversation re-sent at every step. When a budget is reached,
or tool calls keep failing, the agent makes one more call with no tools and writes its answer
from what it has gathered, saying what it didn't get to check. A follow-up in the same chat
("summarise", "and lab2?") sees the previous answer and the previous run's results, so it can
answer without collecting everything again.

## Troubleshooting

- **No Agent tab** — `AGENT_ENABLED` isn't `1`, or you aren't logged in.
- **"Set a dashboard password first"** — Settings → Security.
- **Model says "no tool calls"** — pick another model; Qwen 2.5/3, Llama 3.1+, Mistral Nemo and most
  cloud models call tools.
- **"key rejected"** on an automation — the provider returned 401/403; paste the key again.
- **Forgot the password** — `docker exec homelab-monitor python -m backend.auth.cli reset-password`.
  This also switches the agent off and pauses automations.
- **Two monitors (e.g. prod and dev) on one fleet** — each has its own schedules and locks; don't
  activate the same automation on both.
