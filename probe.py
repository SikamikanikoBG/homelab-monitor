#!/usr/bin/env python3
"""HomeLab Monitor — run-on-remote probe.

The hub pipes this file over SSH (`ssh host python3 -`) every poll cycle.
Nothing persists on the remote: stdin is the script, stdout is one JSON blob,
exit code is 0 on partial-but-useful output. Plain stdlib only — works on any
Linux with Python 3.6+.

JSON shape is a deliberate subset of the hub's own /api/health.now block so the
UI can render local and remote with the same code paths.
"""
import json, os, re, socket, subprocess, sys, time, glob

# ss listen-row parser, mirrored from app.py so service ports can be attributed
# on the remote without depending on the iproute2 *Python* bindings.
_LISTEN_RE = re.compile(r"^LISTEN\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+\s+users:\((?P<users>.*)\)\s*$")
_LISTEN_PORT_RE = re.compile(r":(\d+)$")
_LISTEN_PID_RE = re.compile(r"pid=(\d+)")


def read_loadavg():
    try:
        a, b, c, *_ = open("/proc/loadavg").read().split()
        return {"load1": float(a), "load5": float(b), "load15": float(c)}
    except Exception:
        return {}


def read_meminfo():
    try:
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if not v:
                    continue
                m[k.strip()] = int(v.strip().split()[0])  # kB
        total = m.get("MemTotal", 0)
        avail = m.get("MemAvailable", m.get("MemFree", 0) + m.get("Cached", 0))
        used = max(0, total - avail)
        # MB to match the hub's units.
        return {"ram_total": total // 1024, "ram_used": used // 1024}
    except Exception:
        return {}


def read_uptime():
    try:
        up, _ = open("/proc/uptime").read().split()
        return {"uptime": int(float(up))}
    except Exception:
        return {}


def _cpu_snapshot():
    """Sum across the aggregate `cpu` line of /proc/stat. Returns (total, idle)."""
    parts = open("/proc/stat").readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def read_cpu():
    """Sample twice with a short pause, return delta % busy."""
    try:
        t1, i1 = _cpu_snapshot()
        time.sleep(0.4)
        t2, i2 = _cpu_snapshot()
        td = t2 - t1
        idd = i2 - i1
        if td <= 0:
            return {}
        pct = max(0.0, min(100.0, (td - idd) * 100.0 / td))
        return {"cpu": round(pct, 1), "cores": os.cpu_count() or 1}
    except Exception:
        return {}


def read_temp():
    """Hottest plausible CPU temp from /sys/class/thermal. Coarse, but matches
    what the hub itself reports for its own box (app.py takes the max zone too).

    Taking the *max* — not the first zone — matters on boxes that expose an
    `acpitz` board/ambient sensor alongside the real `x86_pkg_temp`/`coretemp`
    die sensor. `acpitz` sorts first (thermal_zone0) and reads ~ambient, so the
    old first-match logic under-reported the CPU by 20-30 °C (e.g. a Celeron
    reading 28 °C off acpitz while its package sat at 59 °C)."""
    best = None
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                t = int(open(z).read().strip()) / 1000.0
                if 10 < t < 130 and (best is None or t > best):
                    best = t
            except Exception:
                continue
    except Exception:
        pass
    return {"ctemp": round(best, 1)} if best is not None else {}


def read_disks():
    """Real filesystem mounts only — skip overlay/squashfs/tmpfs noise."""
    real_fs = {"ext4", "ext3", "xfs", "btrfs", "zfs", "vfat", "f2fs"}
    seen = set()
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                _, mnt, fst = parts[:3]
                if fst not in real_fs:
                    continue
                if mnt in seen:
                    continue
                seen.add(mnt)
                try:
                    s = os.statvfs(mnt)
                    total = s.f_blocks * s.f_frsize
                    free = s.f_bavail * s.f_frsize
                    used = total - free
                    if total <= 0:
                        continue
                    out.append({
                        "mount": mnt,
                        "total": round(total / (1024 ** 3), 1),
                        "used":  round(used  / (1024 ** 3), 1),
                        "pct":   round(used * 100 / total, 1),
                    })
                except Exception:
                    continue
    except Exception:
        pass
    out.sort(key=lambda d: d["mount"])
    return out


def _collect_listen_ports():
    """{pid: [ports]} from `ss -Hlntp` (IPv4 + IPv6). Best-effort: if `ss`
    isn't installed on the remote, returns {} and ports just stay empty."""
    by_pid = {}
    for args in (["ss", "-Hlntp"], ["ss", "-Hln6tp"]):
        try:
            r = subprocess.run(args, capture_output=True, timeout=3)
        except Exception:
            continue
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            m = _LISTEN_RE.match(ln)
            if not m:
                continue
            pm = _LISTEN_PORT_RE.search(m.group("local"))
            if not pm:
                continue
            port = int(pm.group(1))
            for pid in (int(x) for x in _LISTEN_PID_RE.findall(m.group("users"))):
                by_pid.setdefault(pid, set()).add(port)
    return {pid: sorted(ports) for pid, ports in by_pid.items()}


def _systemd_props(unit):
    """systemctl show on a single unit, parsed into a flat key=value dict."""
    try:
        r = subprocess.run(
            ["systemctl", "show", "--no-pager", unit,
             "-p", "MainPID", "-p", "MemoryCurrent",
             "-p", "ActiveEnterTimestampMonotonic", "-p", "ExecMainStatus"],
            capture_output=True, timeout=3,
        )
    except Exception:
        return {}
    out = {}
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def read_systemd():
    """Inventory systemd services via the CLI — no D-Bus client needed on the
    remote. Output matches the hub's HH.systemd shape so the existing Services
    tab renderer can show this verbatim.

    Returns only admin-deployed (`/etc/systemd/system/*.service`) units and any
    failed unit, mirroring how the local renderer filters its table — the full
    list of vendor services would just be noise."""
    try:
        r = subprocess.run(
            ["systemctl", "--no-pager", "--no-legend", "--plain",
             "list-units", "--type=service", "--all"],
            capture_output=True, timeout=6,
        )
        if r.returncode != 0:
            return {}
    except Exception:
        return {}

    admin_units = set()
    try:
        for f in os.listdir("/etc/systemd/system"):
            if f.endswith(".service"):
                admin_units.add(f)
    except Exception:
        pass

    loaded = running = failed = admin_total = 0
    services = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if not s:
            continue
        # Some systemd versions add a bullet at the start for failed/etc.
        if s[:2] in ("● ", "* "):
            s = s[2:].lstrip()
        parts = s.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[:4]
        desc = parts[4] if len(parts) > 4 else ""
        if not unit.endswith(".service"):
            continue
        if load == "loaded":   loaded  += 1
        if active == "active" and sub == "running": running += 1
        if active == "failed": failed  += 1
        is_admin = unit in admin_units
        if is_admin: admin_total += 1

        if active == "failed":
            status = "crit"
        elif active == "active" and sub == "running":
            status = "ok"
        elif active == "inactive":
            status = "info"
        else:
            status = "warn"

        if is_admin or status == "crit":
            services.append({
                "name": unit, "status": status,
                "active": active, "sub": sub, "desc": desc,
                "admin": is_admin, "watched": False,
            })

    # Enrich only the shown rows (admin/failed) — calling `systemctl show` per
    # unit on the full list would be a lot of fork-exec on a busy host.
    try:
        boot_uptime_s = float(open("/proc/uptime").read().split()[0])
    except Exception:
        boot_uptime_s = 0
    listen = _collect_listen_ports()
    for s in services:
        props = _systemd_props(s["name"])
        try:
            pid = int(props.get("MainPID") or 0)
        except ValueError:
            pid = 0
        try:
            mem = int(props.get("MemoryCurrent") or 0)
        except ValueError:
            mem = 0
        # 2^64-1 is systemd's sentinel for "no accounting / unset".
        s["mem_bytes"] = mem if 0 < mem < 0xFFFFFFFFFFFFFFFF else None
        try:
            enter_us = int(props.get("ActiveEnterTimestampMonotonic") or 0)
        except ValueError:
            enter_us = 0
        s["uptime_s"] = max(0, int(boot_uptime_s - enter_us / 1_000_000)) if (enter_us and boot_uptime_s) else 0
        if s["status"] == "crit":
            try:
                s["exit_status"] = int(props.get("ExecMainStatus", "0"))
            except ValueError:
                pass
        s["ports"] = listen.get(pid, []) if pid else []

    # Failed first, then admin units (running first), alphabetical within.
    def k(x):
        if x["status"] == "crit": return (0, x["name"])
        if x["status"] == "ok":   return (1, x["name"])
        return (2, x["name"])
    services.sort(key=k)

    return {"systemd": {
        "available": True,
        "summary": {"loaded": loaded, "running": running,
                    "failed": failed, "admin": admin_total},
        "services": services,
    }}


def read_gpu():
    """First NVIDIA GPU's snapshot via nvidia-smi. Returns {} if no driver or
    no GPU. We treat the first GPU as the 'representative' for the table; the
    detailed per-GPU view lives in the future GPU tab."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            return {}
        lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        if not lines:
            return {}
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 5:
            return {}
        return {"gpu": {
            "count":     len(lines),
            "name":      parts[4],
            "mem_used":  int(parts[0]),   # MB
            "mem_total": int(parts[1]),   # MB
            "util":      int(parts[2]),   # %
            "temp":      int(parts[3]),   # °C
        }}
    except Exception:
        return {}


def main():
    data = {
        "host": {
            **read_cpu(),
            **read_meminfo(),
            **read_loadavg(),
            **read_uptime(),
            **read_temp(),
            **read_gpu(),
            **read_systemd(),
            "disks": read_disks(),
            "hostname": socket.gethostname(),
        },
        "at": int(time.time()),
        "probe_version": "0.5",
    }
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
