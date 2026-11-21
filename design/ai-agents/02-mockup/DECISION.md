# Gate A decision — 2026-10-24

Mockup: `mockup.html` (published privately at https://claude.ai/artifact/FHVdFaBxcSabQAHVMQjSrY).

**Chosen: Option C "Agent cockpit" + a floating chat widget.**

- One **Agent** entry in the sidebar (new `agents` zone), badge = pending approvals.
  Inside it, tabs: **Chat** (default) · **Automations** · **Activity** · **Agent policy** · **Models & keys**.
- Chat tab (desktop) = 3 columns: conversations + automation runs | chat + composer with model picker |
  right rail with pending approvals (approve/reject inline), fleet access (Off/Passive/Active per host,
  link to policy) and next scheduled runs. On phone width it collapses to the chat column; the rail
  content stays reachable (approvals via the badge/Approvals list, policy via its tab).
- **Floating widget (owner addition):** a round "Ask" button fixed bottom-right on **every tab,
  including Overview**. Click → compact chat panel opens over the page immediately (no navigation),
  aware of the current tab and host as context. It shares conversations with the cockpit
  ("Open in Agent" link expands it). Confirm cards work inside the widget. Hidden when the agent is
  disabled; on phone it opens full-screen. Must not cover existing bottom-right UI (check toasts,
  display mode `/?display=1` → widget hidden in kiosk mode).
- All other screens as in the mockup: template gallery + Activate dialog (hosts, schedule, mode,
  model, delivery), policy page (per-box tri-state + per-action Deny/Ask/Allow with an Unattended
  column, medium/high locked to Ask unattended), Activity list + run detail timeline, Models & keys
  (discovered local models with tool-call badge, cloud providers with own key).
