# Security posture

The Security tab provides a read-only view of each host's security posture. It
collects the state that can be determined safely and surfaces unknown or
permission-limited results instead of guessing.

It is intended as a quick operational overview, not as a vulnerability scanner
or a replacement for a security audit.

## What is checked

| Check | What HomeLab Monitor reports |
|---|---|
| Firewall | Detects UFW, firewalld, nftables, or iptables and reports whether the detected firewall appears active when that state can be determined. |
| SSH | Reports the effective or configured `PermitRootLogin` and `PasswordAuthentication` values, along with the SSH port when available. |
| SELinux | Reports enforcing, permissive, or disabled state when detectable. |
| AppArmor | Reports whether AppArmor appears enabled or disabled. |
| fail2ban | Reports whether fail2ban is installed and, when its service state can be queried, whether it is active. |
| Reboot required | Checks whether the host reports that a reboot is required. |
| Automatic updates | Checks configured unattended or automatic update mechanisms where supported. |
| Pending updates | Reports cached package-update information without forcing a package-manager refresh. |

The dashboard surfaces potentially concerning states first. A result such as
`unknown` or `needs elevated read` does not mean that a feature is disabled; it
means HomeLab Monitor could not determine its state with the access available.

## Firewall detection

For remote Linux hosts, the probe checks supported firewall backends in order:
UFW, firewalld, nftables, then iptables.

For UFW and firewalld, the probe queries their command-line tools. For nftables
and iptables, it inspects the current ruleset when permitted. If a command
exists but its state cannot be read, the result remains unknown rather than
being treated as inactive.

The local hub is more conservative because it runs inside the monitor
container. It reads host-mounted configuration where possible instead of
assuming that tools installed inside the container represent the host.

## SSH hardening

The remote probe first tries:

```bash
sshd -T
```

This exposes the effective sshd configuration, including defaults. If that
cannot be used, HomeLab Monitor falls back to parsing /etc/ssh/sshd_config.

The Security tab reports these settings when available:

- PermitRootLogin
- PasswordAuthentication
- SSH port

For the local host, the monitor reads the host's mounted sshd_config rather
than querying the container's SSH environment.

## SELinux and AppArmor

SELinux state is read from the kernel interface when available, with
getenforce used as a fallback by the remote probe. The result may be
enforcing, permissive, or disabled.

AppArmor state is determined from its kernel module state and security
filesystem.

These mechanisms are Linux-specific. Hosts where they do not apply may show a
neutral or unavailable state.

## fail2ban

HomeLab Monitor detects fail2ban from its binaries or systemd service files.

On a remote host it also asks systemd whether the service is active. If
fail2ban is installed but its service state cannot be read, the dashboard keeps
that state unknown instead of reporting it as inactive.

For the local host, installation can be detected from the mounted host
filesystem, but service activity is not assumed from inside the container.

## Reboot required

On systems that expose /var/run/reboot-required or
/run/reboot-required, the presence of either file marks a reboot as pending.

Remote hosts can additionally use needs-restarting -r when available.

## Automatic and pending updates

For Debian/Ubuntu-style systems, HomeLab Monitor can read the unattended-upgrade
configuration to determine whether automatic upgrades are enabled.

Remote probes can also recognise enabled update-related systemd units such as
unattended-upgrades.service, dnf-automatic.timer, and
apt-daily-upgrade.timer.

Pending-update collection is deliberately passive. The probe reads package
manager information that the host has already cached; it does not trigger a
network refresh just to populate the Security tab. Fields that cannot be
determined remain unknown rather than being reported as zero.

## Local and remote hosts

Security information is collected slightly differently depending on where the
host runs.

Remote Linux hosts are inspected by the probe running on that host, so it can
use host commands such as firewall tools and systemd where available.

The local hub runs inside a container. For host-level security information it
therefore prefers files exposed through the read-only host mount and avoids
treating the container's own environment as if it were the host.

This distinction is why some local results may be unknown even when the
corresponding remote probe can determine them.


## Dashboard login

The dashboard has an **optional** login. Until you set a password nothing
changes: every page and API answers as it always did, which is fine on a
trusted LAN. Set one under **Settings → Security** (or `ADMIN_PASSWORD` /
`ADMIN_PASSWORD_FILE` at boot) and:

- browsers log in at `/login` and get a server-side session (the cookie is
  `HttpOnly`, `SameSite=Lax`, and only its hash is stored); changing the
  password signs every other browser out;
- every change (POST/PUT/PATCH/DELETE) from a browser needs the session's CSRF
  token and a same-origin request — the dashboard adds it for you
  (`TRUSTED_ORIGINS` lists extra origins for reverse proxies);
- scripts use an **API key with a scope** (Settings → Integrations → API keys):
  `push` only reaches the experiment push routes (every key made before the
  login existed is a push key), `read` reads everything except the backup and
  key listings (Grafana, Homepage, an MCP client), `admin` does everything and
  expires after 90 days by default. Read and admin keys can only be made once a
  password exists; setting the first password revokes any you don't keep;
- open without a login: `/healthz`, `/metrics` (set `METRICS_REQUIRE_AUTH=1`
  to require a read key), the public status page and the login page;
- after 5 wrong passwords an address waits a growing delay (up to a minute).

Forgot it? `docker exec homelab-monitor python -m backend.auth.cli reset-password`
clears the password and every session (and switches the AI agent off).

**Backups don't carry secrets.** `/api/backup` blanks the alert channels'
webhook URLs, tokens and passwords, AI provider keys, the password hash and all
sessions in the downloaded copy. After a restore, re-enter them. API keys are
kept: the database only holds their SHA-256 hashes (the keys themselves are
shown once, at creation, and never stored), so a backup can't be used to
recover a key — and the push keys your notebooks use keep working after a
restore. Revoke a key under Integrations → API keys if a backup went somewhere
it shouldn't.

With a login set, responses also carry `X-Frame-Options: SAMEORIGIN` and
`Content-Security-Policy: frame-ancestors 'self'`, so no other site can frame
the logged-in dashboard. Without a login, responses are unchanged.

**`/api/hosts/<name>/run`** (run any command on a host; nothing in the UI uses
it) is off unless `ENABLE_HOST_RUN=1`, and every use is logged without the
command text.

## AI agent

The optional [AI agent](agents.md) can only run behind the login. What it may
do is decided on the server, per host (Off / Passive / Active) and per action
(Deny / Ask / Allow), checked when it proposes and again when it executes. It
has no shell: only a fixed catalogue of named actions with validated, quoted
parameters. Tool results are treated as untrusted data, so a container named
"ignore previous instructions" can at most make it *propose* something you
then see on a card. What Passive and Off protect: they bind this in-app agent.
They don't bind the read-only MCP server (it still sees the whole fleet) or a
person using the dashboard's own buttons (those follow `ENABLE_CONTROLS`).

## API and multi-host access

API-protected surfaces accept an API key either as a Bearer token in the
`Authorization` header or through `X-API-Key`. Missing, invalid, or expired
keys are rejected with HTTP 401.

Multi-host monitoring uses SSH public-key authentication rather than stored
password authentication. Remote probes use bounded timeouts so an unavailable
or slow host does not block the monitoring loop.


## Read-only by design

The posture checks only inspect system state. They do not enable a firewall,
change SSH configuration, start fail2ban, install updates, or reboot a host.

HomeLab Monitor's MCP server is also read-only (with a login set, its HTTP
port needs a read key too — see [MCP](mcp.md)). It exposes host posture through
the monitor's existing read-only APIs but does not expose the dashboard's
container/service controls or self-update actions.

Security status should therefore be treated as information for an
administrator to review, not as an automatic remediation system.

!!! warning "Keep HomeLab Monitor private"

    HomeLab Monitor has broad visibility into its hosts. Keep the dashboard and
    MCP server behind your LAN, VPN, or firewall rather than exposing them
    directly to the public internet.

