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

This exposes the effective sshd configuration, including defaults. If that
cannot be used, HomeLab Monitor falls back to parsing /etc/ssh/sshd_config.

The Security tab reports these settings when available:

PermitRootLogin
PasswordAuthentication
SSH port

For the local host, the monitor reads the host's mounted sshd_config rather
than querying the container's SSH environment.

SELinux and AppArmor

SELinux state is read from the kernel interface when available, with
getenforce used as a fallback by the remote probe. The result may be
enforcing, permissive, or disabled.

AppArmor state is determined from its kernel module state and security
filesystem.

These mechanisms are Linux-specific. Hosts where they do not apply may show a
neutral or unavailable state.

fail2ban

HomeLab Monitor detects fail2ban from its binaries or systemd service files.

On a remote host it also asks systemd whether the service is active. If
fail2ban is installed but its service state cannot be read, the dashboard keeps
that state unknown instead of reporting it as inactive.

For the local host, installation can be detected from the mounted host
filesystem, but service activity is not assumed from inside the container.

Reboot required

On systems that expose /var/run/reboot-required or
/run/reboot-required, the presence of either file marks a reboot as pending.

Remote hosts can additionally use needs-restarting -r when available.

Automatic and pending updates

For Debian/Ubuntu-style systems, HomeLab Monitor can read the unattended-upgrade
configuration to determine whether automatic upgrades are enabled.

Remote probes can also recognise enabled update-related systemd units such as
unattended-upgrades.service, dnf-automatic.timer, and
apt-daily-upgrade.timer.

Pending-update collection is deliberately passive. The probe reads package
manager information that the host has already cached; it does not trigger a
network refresh just to populate the Security tab. Fields that cannot be
determined remain unknown rather than being reported as zero.

Local and remote hosts

Security information is collected slightly differently depending on where the
host runs.

Remote Linux hosts are inspected by the probe running on that host, so it can
use host commands such as firewall tools and systemd where available.

The local hub runs inside a container. For host-level security information it
therefore prefers files exposed through the read-only host mount and avoids
treating the container's own environment as if it were the host.

This distinction is why some local results may be unknown even when the
corresponding remote probe can determine them.

Read-only by design

The posture checks only inspect system state. They do not enable a firewall,
change SSH configuration, start fail2ban, install updates, or reboot a host.

HomeLab Monitor's MCP server is also read-only. It exposes host posture through
the monitor's existing read-only APIs but does not expose the dashboard's
container/service controls or self-update actions.

Security status should therefore be treated as information for an
administrator to review, not as an automatic remediation system.

!!! warning "Keep HomeLab Monitor private"

HomeLab Monitor has broad visibility into its hosts. Keep the dashboard and
MCP server behind your LAN, VPN, or firewall rather than exposing them
directly to the public internet.

