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
        # Non-reclaimable kernel memory (slab/page-tables/stacks): inside "used" RAM but
        # tied to no container/service, so the hub treemap can carve it out of the
        # "Host & other" bucket. SReclaimable is left out (it counts as available).
        kernel = m.get("SUnreclaim", 0) + m.get("KernelStack", 0) + m.get("PageTables", 0)
        # MB to match the hub's units.
        return {"ram_total": total // 1024, "ram_used": used // 1024,
                "ram_kernel": kernel // 1024}
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


# hwmon drivers that expose the actual CPU die/core sensors (Intel/AMD/ARM).
_CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal")
# thermal_zone *types* that are the CPU (never acpitz/pch/nvme/wifi).
_CPU_ZONE  = ("x86_pkg_temp", "cpu_thermal", "cpu-thermal")

def _cpu_temp_c():
    """CPU temperature in °C that matches what `sensors` shows for the CPU cores.

    The old logic took the max of *every* /sys/class/thermal zone, which on many
    boards grabs a chipset/PCH/NVMe or an mis-calibrated package sensor reading
    10-20 °C hotter than the cores — so the dashboard showed e.g. 51 °C while
    `sensors` showed Core N at 37 °C. We now prefer the coretemp/k10temp hwmon and
    report the *average core* temperature across every CPU package; then a
    CPU-typed thermal zone; and only as a last resort the old hottest-plausible-
    zone (so exotic/ARM boards still report).

    Average, not max: on a many-core server (e.g. a 56-core dual-socket Xeon) a
    single busy core spiking to 45 °C while the other 55 sit at 39 °C should still
    read ~40, matching the bulk of what `sensors` shows — max-of-N-cores is noisy
    and biased high the more cores you have."""
    best = None
    # 1) hwmon coretemp/k10temp/zenpower — the real die/core sensors. Pool the
    #    per-core readings from *every* CPU package and report their average.
    try:
        cores, allt = [], []
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                name = open(hw + "/name").read().strip()
            except Exception:
                continue
            if name not in _CPU_HWMON:
                continue
            for inp in glob.glob(hw + "/temp*_input"):
                try:
                    t = int(open(inp).read().strip()) / 1000.0
                except Exception:
                    continue
                if not (0 < t < 130):
                    continue
                allt.append(t)
                try:
                    lbl = open(inp[:-6] + "_label").read().strip().lower()
                except Exception:
                    lbl = ""
                if lbl.startswith("core"):          # Intel "Core N" — exclude Package
                    cores.append(t)
        pool = cores or allt                        # cores if labelled, else whatever the die reports
        if pool:
            return round(sum(pool) / len(pool), 1)
    except Exception:
        pass
    # 2) thermal zones explicitly typed as the CPU.
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                ztype = open(z + "/type").read().strip().lower()
            except Exception:
                continue
            if ztype in _CPU_ZONE or "cpu" in ztype:
                try:
                    t = int(open(z + "/temp").read().strip()) / 1000.0
                except Exception:
                    continue
                if 0 < t < 130 and (best is None or t > best):
                    best = t
        if best is not None:
            return round(best, 1)
    except Exception:
        pass
    # 3) last resort: hottest plausible zone (original behaviour) so we still
    #    report *something* on boards without coretemp or a CPU-typed zone.
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
    return round(best, 1) if best is not None else None


def read_temp():
    t = _cpu_temp_c()
    return {"ctemp": t} if t is not None else {}


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


def _smi_int(v):
    """Tolerant int for nvidia-smi fields: '[N/A]' / '[Not Supported]' / blank → 0
    so one unreadable field (e.g. temperature on some cards) doesn't drop the
    whole GPU from the remote's report."""
    try:
        return int(float((v or "").strip()))
    except ValueError:
        return 0


# Patched in tests to a fake sysfs tree; the live default is the real path.
AMD_DRM_GLOB = "/sys/class/drm/card*/device"

def _amd_sysfs_int(path, scale=1.0):
    """sysfs integer (optionally scaled: bytes→MB, µW→W, m°C→°C). None on miss/parse
    error so one unreadable attribute degrades that field rather than the whole card."""
    try:
        with open(path) as f:
            return int(float(f.read().strip()) / scale)
    except Exception:
        return None

def _amd_hwmon_temp_power(dev):
    """(temp °C, power W) from a card's hwmon subdir, each best-effort/None."""
    temp = power = None
    try:
        subdirs = sorted(os.listdir(os.path.join(dev, "hwmon")))
    except Exception:
        subdirs = []
    for sub in subdirs:
        hp = os.path.join(dev, "hwmon", sub)
        if temp is None:
            for t in ("temp1_input", "temp2_input", "temp3_input"):
                temp = _amd_sysfs_int(os.path.join(hp, t), scale=1000.0)
                if temp is not None:
                    break
        if power is None:
            for pw in ("power1_average", "power1_input", "power2_average"):
                power = _amd_sysfs_int(os.path.join(hp, pw), scale=1_000_000.0)
                if power is not None:
                    break
        if temp is not None and power is not None:
            break
    return temp, power

def read_amd_gpus():
    """AMD (vendor 0x1002) GPUs from /sys/class/drm via pure file reads — no ROCm.
    Returns a list of per-card dicts {name,util,mem_used(MB),mem_total(MB),temp,power}
    so the hub treats AMD remotes exactly like NVIDIA ones. Empty if none / no sysfs."""
    out = []
    try:
        devs = sorted(glob.glob(AMD_DRM_GLOB))
    except Exception:
        return out
    for dev in devs:
        try:
            vendor = ""
            try:
                with open(os.path.join(dev, "vendor")) as f:
                    vendor = f.read().strip().lower()
            except Exception:
                pass
            if vendor != "0x1002":
                continue
            util      = _amd_sysfs_int(os.path.join(dev, "gpu_busy_percent"))
            mem_used  = _amd_sysfs_int(os.path.join(dev, "mem_info_vram_used"),  scale=1024 * 1024)
            mem_total = _amd_sysfs_int(os.path.join(dev, "mem_info_vram_total"), scale=1024 * 1024)
            if util is None and mem_total is None:
                continue
            name = "AMD GPU"
            for marker in ("product_name", "device"):
                try:
                    with open(os.path.join(dev, marker)) as f:
                        v = f.read().strip()
                    if v:
                        name = ("AMD GPU " + v) if v.lower().startswith("0x") else v
                        break
                except Exception:
                    pass
            temp, power = _amd_hwmon_temp_power(dev)
            out.append({
                "name":      name,
                "util":      util or 0,
                "mem_used":  mem_used or 0,
                "mem_total": mem_total or 0,
                "temp":      temp or 0,
                "power":     power or 0,
            })
        except Exception:
            continue
    return out

def read_gpu():
    """Representative GPU snapshot for the remote's report. Tries NVIDIA via
    nvidia-smi first; if there's no NVIDIA driver/GPU, falls back to AMD cards read
    straight from sysfs (pure stdlib, no ROCm). Returns {} when neither is present.
    The first card is the 'representative' for the table; `count` covers all cards."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
            if lines:
                parts = [p.strip() for p in lines[0].split(",")]
                if len(parts) >= 5:
                    return {"gpu": {
                        "count":     len(lines),
                        "name":      parts[4],
                        "mem_used":  _smi_int(parts[0]),   # MB
                        "mem_total": _smi_int(parts[1]),   # MB
                        "util":      _smi_int(parts[2]),   # %
                        "temp":      _smi_int(parts[3]),   # °C
                    }}
    except Exception:
        pass
    # No NVIDIA — try AMD via sysfs (Linux amdgpu).
    try:
        amd = read_amd_gpus()
        if amd:
            g0 = amd[0]
            return {"gpu": {
                "count":     len(amd),
                "name":      g0["name"],
                "mem_used":  sum(g["mem_used"] for g in amd),
                "mem_total": sum(g["mem_total"] for g in amd),
                "util":      round(sum(g["util"] for g in amd) / len(amd)),
                "temp":      max(g["temp"] for g in amd),
            }}
    except Exception:
        pass
    return {}


# ── System / Hardware / Network / Security inventory ──────────────────────────
# Everything below is slow-changing context (OS, CPU model, NICs, firewall…) that
# the hub renders on the System / Network / Security tabs. All best-effort: every
# reader degrades to a partial dict (or drops the field) rather than raising, so
# the probe works the same on x86_64, aarch64 (Pi), armv7 and i686, with or
# without `ip`, `ss`, dmidecode, systemd or root. Anything that genuinely needs
# root and can't read returns None → the UI shows a neutral "needs elevated read".

def _which(name):
    for p in (os.environ.get("PATH") or "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        if p and os.path.exists(os.path.join(p, name)):
            return os.path.join(p, name)
    return None


def _run(args, timeout=3):
    """subprocess.run → (rc, stdout_str) or (None, '') on any failure. Forces
    LC_ALL/LANG=C so package-manager output (zypper/apt/dnf sentinels like
    'No updates found') and other parsed text stay in English regardless of the
    SSH login shell's locale."""
    try:
        env = dict(os.environ, LC_ALL="C", LANG="C")
        r = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except Exception:
        return None, ""


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def _read_dt_string(*paths):
    """Read a NUL-terminated device-tree string (Raspberry Pi etc.)."""
    for p in paths:
        try:
            v = open(p, "rb").read().decode("utf-8", "replace").strip("\x00").strip()
            if v:
                return v
        except Exception:
            continue
    return None


def _os_release(base=""):
    data = {}
    for path in (base + "/etc/os-release", base + "/usr/lib/os-release"):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
        if data:
            break
    return data


def os_family(osid, id_like="", uname=""):
    """Normalize an os-release ID into a family the remedies/UI branch on.
    Mirrors app.py's _detect_os normalization so both ends agree."""
    osid, id_like, uname = (osid or "").lower(), (id_like or "").lower(), (uname or "").lower()
    if uname == "darwin":
        return "macos"
    if osid == "alpine":
        return "alpine"
    if osid in ("opensuse-leap", "opensuse-tumbleweed", "sles", "sled") or "suse" in id_like:
        return "suse"
    if osid in ("debian", "ubuntu", "raspbian", "pop", "linuxmint") or "debian" in id_like:
        return "debian"
    if osid in ("fedora", "rhel", "centos", "rocky", "almalinux") or "rhel" in id_like or "fedora" in id_like:
        return "rhel"
    if osid in ("arch", "manjaro", "endeavouros") or "arch" in id_like:
        return "arch"
    return "linux"


def _detect_init():
    if os.path.isdir("/run/systemd/system"):
        return "systemd"
    comm = (_read_text("/proc/1/comm") or "").strip()
    if comm == "systemd":
        return "systemd"
    if os.path.exists("/run/openrc") or _which("rc-service"):
        return "openrc"
    return comm or None


def _detect_virt():
    if _which("systemd-detect-virt"):
        # Note: systemd-detect-virt exits 1 on bare metal (output "none"), 0 when
        # a virt tech is detected — so branch on the *output*, not the exit code.
        rc, out = _run(["systemd-detect-virt"], timeout=2)
        v = out.strip()
        if v == "none":
            return "bare-metal"
        if v and rc is not None:
            return v
    if os.path.exists("/.dockerenv"):
        return "docker"
    cg = _read_text("/proc/1/cgroup") or ""
    if "docker" in cg:
        return "docker"
    if "lxc" in cg:
        return "lxc"
    blob = ((_read_text("/sys/class/dmi/id/product_name") or "") + " " +
            (_read_text("/sys/class/dmi/id/sys_vendor") or "")).lower()
    for key, name in (("kvm", "kvm"), ("virtualbox", "virtualbox"), ("vmware", "vmware"),
                      ("qemu", "qemu"), ("xen", "xen"), ("microsoft", "hyper-v")):
        if key in blob:
            return name
    if " hypervisor" in (_read_text("/proc/cpuinfo") or ""):
        return "vm"
    return "bare-metal" if blob.strip() else None


def read_os():
    info = {}
    try:
        u = os.uname()
        info["kernel"] = u.release
        info["arch"] = u.machine
        info["hostname"] = u.nodename
    except Exception:
        pass
    rel = _os_release()
    osid = (rel.get("ID") or "").lower()
    info["id"] = osid or None
    info["pretty"] = rel.get("PRETTY_NAME") or rel.get("NAME") or None
    info["version_id"] = rel.get("VERSION_ID") or None
    info["family"] = os_family(osid, rel.get("ID_LIKE"))
    info["init"] = _detect_init()
    info["virt"] = _detect_virt()
    try:
        info["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    for line in (_read_text("/proc/stat") or "").splitlines():
        if line.startswith("btime"):
            try:
                info["boot_time"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
            break
    pretty = info.get("pretty") or osid or info.get("kernel") or "unknown"
    info["label"] = pretty + (" · " + info["init"] if info.get("init") else "")
    return {"os": {k: v for k, v in info.items() if v is not None}}


def _count_physical_cores(cpuinfo):
    pairs, cur = set(), None
    for line in cpuinfo.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "physical id":
            cur = v
        elif k == "core id":
            pairs.add((cur, v))
    return len(pairs)


def read_hw():
    hw = {}
    ci = _read_text("/proc/cpuinfo") or ""
    mname = arm = vendor = None
    phys = set()
    for line in ci.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if not v:
            continue
        if k == "model name" and not mname:        # x86 / most ARM full CPU string
            mname = v
        elif k == "vendor_id" and not vendor:
            vendor = v
        elif k == "physical id":
            phys.add(v)
        elif k == "hardware" and not arm:          # ARM SoC name (no "model name")
            arm = v
    # Never use the numeric x86 "model :" field; fall back arm-field → device tree.
    model = mname or arm or _read_dt_string("/proc/device-tree/model",
                                            "/sys/firmware/devicetree/base/model")
    threads = os.cpu_count() or 1
    cores = _count_physical_cores(ci) or threads
    if model:
        hw["cpu_model"] = model
    if vendor:
        hw["cpu_vendor"] = vendor
    hw["sockets"] = len(phys) or 1
    hw["cores"] = cores
    hw["threads"] = threads
    khz = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        hw["cpu_mhz_max"] = round(int(khz.strip()) / 1000)
    except (AttributeError, ValueError):
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", ci)
        if m:
            hw["cpu_mhz_max"] = round(float(m.group(1)))
    try:
        mi = {}
        for line in (_read_text("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            if v:
                mi[k.strip()] = int(v.split()[0])
        if mi.get("MemTotal"):
            hw["ram_total"] = mi["MemTotal"] // 1024     # MB
        if mi.get("SwapTotal"):
            hw["swap_total"] = mi["SwapTotal"] // 1024
    except Exception:
        pass
    machine = (_read_text("/sys/class/dmi/id/product_name") or "").strip()
    if machine.lower() in ("", "to be filled by o.e.m.", "system product name", "default string"):
        machine = _read_dt_string("/proc/device-tree/model",
                                  "/sys/firmware/devicetree/base/model") or ""
    if machine:
        hw["machine"] = machine
    g = read_gpu().get("gpu")
    if g and g.get("name"):
        hw["gpu_name"] = g["name"]
        if g.get("mem_total"):
            hw["gpu_mem_total"] = g["mem_total"]
    return {"hw": hw}


def _iface_type(name, d):
    if name == "lo":
        return "loopback"
    if os.path.isdir(d + "/wireless"):
        return "wifi"
    if name.startswith("wg"):
        return "wireguard"
    if name.startswith(("tun", "tap")):
        return "tunnel"
    if name.startswith(("docker", "br-", "veth", "virbr")):
        return "virtual"
    if name.startswith("bond"):
        return "bond"
    if os.path.exists(d + "/device"):
        return "ethernet"
    return "other"


def _net_ifaces():
    out = []
    base = "/sys/class/net"
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for n in names:
        # Skip container plumbing: per-container veth pairs and Docker's
        # per-network bridges (br-<hex>) are pure noise and dominate the list
        # on any Docker host. Keep docker0, named bridges (br0), eth*, wg*, etc.
        if n.startswith("veth") or re.match(r"br-[0-9a-f]{8,}$", n):
            continue
        d = base + "/" + n
        rd = lambda f: (_read_text(d + "/" + f) or "").strip() or None
        iface = {"name": n, "ipv4": [], "ipv6": [], "type": _iface_type(n, d)}
        mac = rd("address")
        if mac and mac != "00:00:00:00:00:00":
            iface["mac"] = mac
        st = rd("operstate")
        if st:
            iface["state"] = st
        try:
            iface["mtu"] = int(rd("mtu"))
        except (TypeError, ValueError):
            pass
        try:
            sp = int(rd("speed"))           # -1 / raises on virtual or down links
            if sp > 0:
                iface["speed_mbps"] = sp
        except (TypeError, ValueError):
            pass
        for stat, key in (("statistics/rx_bytes", "rx_bytes"), ("statistics/tx_bytes", "tx_bytes")):
            try:
                iface[key] = int(rd(stat))
            except (TypeError, ValueError):
                pass
        out.append(iface)
    return out


def _default_route():
    """(iface, gateway_ipv4) for the default route, or (None, None)."""
    for line in (_read_text("/proc/net/route") or "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[1] == "00000000":
            try:
                gw = ".".join(str(int(p[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
                return p[0], gw
            except ValueError:
                return p[0], None
    return None, None


def _primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # no packet is sent — just picks the source IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _collect_ips(ifaces):
    """Populate each iface's ipv4/ipv6 via `ip -o addr` when present. Falls back
    to attaching just the primary source IP to its iface if iproute2 is absent."""
    idx = {i["name"]: i for i in ifaces}
    rc, out = _run(["ip", "-o", "addr", "show"], timeout=3)
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
                i = idx.get(parts[1])
                if not i:
                    continue
                addr = parts[3].split("/")[0]
                if parts[2] == "inet":
                    i["ipv4"].append(addr)
                elif not addr.startswith("fe80"):
                    i["ipv6"].append(addr)
        return
    pip = _primary_ip()
    route_if, _ = _default_route()
    if pip and route_if and route_if in idx and pip not in idx[route_if]["ipv4"]:
        idx[route_if]["ipv4"].append(pip)


def _dns():
    ns, search = [], []
    for line in (_read_text("/etc/resolv.conf") or "").splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) > 1:
                ns.append(parts[1])
        elif line.startswith(("search", "domain")):
            search += line.split()[1:]
    return ns, search


_SS_NAME_RE = re.compile(r'"([^"]+)",pid=\d+')


def _hex_to_ip(h, fam):
    try:
        if fam == 4:
            return socket.inet_ntoa(bytes(int(h[i:i + 2], 16) for i in (6, 4, 2, 0)))
        raw = bytes(int(h[i:i + 2], 16) for i in range(0, 32, 2))
        return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in (0, 4, 8, 12)))
    except Exception:
        return h


def _listen_from_proc():
    """Fallback listen list straight from /proc/net/{tcp,udp}[6] — used when `ss`
    isn't installed or isn't on the non-interactive PATH (common on minimal or
    old hosts). No process attribution, but bind address + exposure still work."""
    out, seen = [], set()
    for path, proto, fam, want in (("/proc/net/tcp", "tcp", 4, "0A"), ("/proc/net/tcp6", "tcp", 6, "0A"),
                                   ("/proc/net/udp", "udp", 4, "07"), ("/proc/net/udp6", "udp", 6, "07")):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 4 or cols[3] != want or ":" not in cols[1]:
                continue
            hexip, hexport = cols[1].rsplit(":", 1)
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            allzero = set(hexip) <= {"0"}
            addr = (("0.0.0.0" if fam == 4 else "::") if allzero else _hex_to_ip(hexip, fam))
            key = (proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            out.append({"proto": proto, "addr": addr, "port": port, "exposed": allzero, "proc": None})
    return out


def _listen_sockets():
    """Listening TCP/UDP sockets with bind address + owning process, from `ss`.
    Each row flags `exposed` when bound to all interfaces (0.0.0.0 / ::) — the
    signal the Security tab uses to show which services face the network. Falls
    back to /proc/net parsing when `ss` is unavailable."""
    socks, seen = [], set()
    for args in (["ss", "-Hlntp"], ["ss", "-Hln6tp"], ["ss", "-Hlunp"], ["ss", "-Hlun6p"]):
        proto = "tcp" if "t" in args[1] else "udp"
        rc, out = _run(args, timeout=3)
        if rc is None:
            continue
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            pm = _LISTEN_PORT_RE.search(local)
            if not pm:
                continue
            port = int(pm.group(1))
            addr = local[:pm.start()].rstrip(":") or "*"
            key = (proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            nm = _SS_NAME_RE.search(ln)
            socks.append({"proto": proto, "addr": addr, "port": port,
                          "exposed": addr in ("0.0.0.0", "*", "::", "[::]"),
                          "proc": nm.group(1) if nm else None})
    if not socks:
        socks = _listen_from_proc()
    socks.sort(key=lambda s: (not s["exposed"], s["port"]))
    return socks


def _established_count():
    n = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) > 3 and cols[3] == "01":   # 01 = ESTABLISHED
                n += 1
    return n


def read_net():
    net = {}
    ifaces = _net_ifaces()
    _collect_ips(ifaces)
    route_if, gw = _default_route()
    if gw:
        net["gateway"] = gw
    pip = _primary_ip()
    if pip:
        net["primary_ip"] = pip
    primary = route_if or next((i["name"] for i in ifaces if pip and pip in i["ipv4"]), None)
    if primary:
        net["primary_iface"] = primary
    ns, search = _dns()
    if ns:
        net["dns"] = ns
    if search:
        net["search"] = search
    try:
        net["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    net["ifaces"] = ifaces
    net["listen"] = _listen_sockets()
    net["established_count"] = _established_count()
    return {"net": net}


def _firewall():
    if _which("ufw"):
        rc, out = _run(["ufw", "status"], timeout=3)
        o = out.lower()
        if rc == 0 and "status: active" in o:
            return {"backend": "ufw", "active": True, "open_ports": o.count("allow") or None}
        if rc == 0 and "status: inactive" in o:
            return {"backend": "ufw", "active": False}
        return {"backend": "ufw", "active": None}        # needs root
    if _which("firewall-cmd"):
        rc, out = _run(["firewall-cmd", "--state"], timeout=3)
        if rc is not None and out.strip() in ("running", "not running"):
            return {"backend": "firewalld", "active": out.strip() == "running"}
        return {"backend": "firewalld", "active": None}
    if _which("nft"):
        rc, out = _run(["nft", "list", "ruleset"], timeout=3)
        if rc == 0:
            return {"backend": "nftables", "active": bool(out.strip())}
        return {"backend": "nftables", "active": None}
    if _which("iptables"):
        rc, out = _run(["iptables", "-S"], timeout=3)
        if rc == 0:
            return {"backend": "iptables",
                    "active": any(l.startswith("-A") for l in out.splitlines())}
        return {"backend": "iptables", "active": None}
    return {"backend": None, "active": False}            # none detected


def _ssh_config():
    cfg = {}
    sshd = _which("sshd")
    if sshd:
        rc, out = _run([sshd, "-T"], timeout=3)          # resolves defaults; needs root
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    cfg[parts[0].lower()] = parts[1].strip()
    if not cfg:
        for line in (_read_text("/etc/ssh/sshd_config") or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    cfg.setdefault(parts[0].lower(), parts[1].strip())
    if not cfg:
        return None
    out = {}
    if "permitrootlogin" in cfg:
        out["permit_root"] = cfg["permitrootlogin"].split()[0]
    if "passwordauthentication" in cfg:
        out["password_auth"] = cfg["passwordauthentication"].split()[0]
    try:
        out["port"] = int(cfg["port"].split()[0])
    except (KeyError, ValueError):
        pass
    return out or None


def _selinux():
    if os.path.exists("/sys/fs/selinux/enforce"):
        v = (_read_text("/sys/fs/selinux/enforce") or "").strip()
        return "enforcing" if v == "1" else "permissive" if v == "0" else None
    if _which("getenforce"):
        rc, out = _run(["getenforce"], timeout=2)
        return (out.strip().lower() or "disabled") if rc is not None else None
    return "disabled"


def _apparmor():
    v = (_read_text("/sys/module/apparmor/parameters/enabled") or "").strip()
    if v:
        return "enabled" if v in ("Y", "y") else "disabled"
    return "enabled" if os.path.isdir("/sys/kernel/security/apparmor") else "disabled"


def _fail2ban():
    installed = bool(_which("fail2ban-client") or _which("fail2ban-server")
                     or os.path.exists("/lib/systemd/system/fail2ban.service")
                     or os.path.exists("/etc/systemd/system/fail2ban.service"))
    if not installed:
        return {"installed": False}
    active = None
    rc, out = _run(["systemctl", "is-active", "fail2ban"], timeout=3)
    if rc is not None and out.strip() in ("active", "inactive", "failed"):
        active = out.strip() == "active"
    return {"installed": True, "active": active}


def _reboot_required():
    if os.path.exists("/var/run/reboot-required") or os.path.exists("/run/reboot-required"):
        return True
    if _which("needs-restarting"):
        rc, _out = _run(["needs-restarting", "-r"], timeout=5)
        return rc == 1 if rc is not None else None        # rc 1 = reboot needed
    return False


def _auto_updates():
    txt = _read_text("/etc/apt/apt.conf.d/20auto-upgrades")
    if txt:
        m = re.search(r'Unattended-Upgrade"\s+"(\d+)"', txt)
        if m:
            return m.group(1) != "0"
    for unit in ("unattended-upgrades.service", "dnf-automatic.timer", "apt-daily-upgrade.timer"):
        rc, out = _run(["systemctl", "is-enabled", unit], timeout=3)
        if rc is not None and out.strip() == "enabled":
            return True
    if _which("unattended-upgrade") or _which("dnf-automatic"):
        return False
    return None


# ── Pending package updates ───────────────────────────────────────────────────
# Strictly cached / offline: we read what the host's package manager already
# computed (its daily timer), never triggering a network refresh and never
# assuming root. Each reader returns {count, security, kernel, source}; any field
# we can't determine stays None so the UI shows a neutral "needs elevated read"
# instead of a misleading zero. The hub adds the "newer OS release available"
# signal separately (it needs the network, which the probe deliberately avoids).

def _parse_updates_file(txt):
    """Parse update-notifier's pre-rendered text. Its wording varies across
    releases ('N updates can be applied immediately.' / 'M of these updates are
    standard security updates.'), so we go line-by-line: the line that mentions
    security gives the security count, the first other count-bearing line gives
    the total. Returns (count, security), either possibly None."""
    count = security = None
    for line in txt.splitlines():
        m = re.search(r"(\d+)", line)
        if not m:
            continue
        n, low = int(m.group(1)), line.lower()
        if "securit" in low:
            security = n
        elif count is None and ("update" in low or "package" in low or "can be" in low):
            count = n
    return count, security


def _apt_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "apt"}
    # update-notifier pre-renders the counts (with the security split) into a file
    # any user can read — no apt invocation, no lock, no refresh.
    txt = _read_text("/var/lib/update-notifier/updates-available")
    if txt:
        out["count"], out["security"] = _parse_updates_file(txt)
    # If the file was missing (no update-notifier) or we still need the kernel
    # signal it can't give, fall back to the cached upgradable list. `apt list
    # --upgradable` reads only the on-disk lists — it does not hit the network.
    if out["count"] is None or out["kernel"] is None:
        rc, txt2 = _run(["apt", "list", "--upgradable"], timeout=6)
        if rc is not None and txt2:
            pkgs = [l for l in txt2.splitlines()
                    if "/" in l.split(" ", 1)[0] and "]" in l]
            if out["count"] is None:
                out["count"] = len(pkgs)
            if out["security"] is None:
                out["security"] = sum(1 for l in pkgs if "-security" in l.lower())
            out["kernel"] = any(l.split("/", 1)[0].startswith(("linux-image", "linux-generic"))
                                for l in pkgs)
    return out


def _zypper_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "zypper"}
    # --no-refresh keeps it offline; status column 'v' marks an available update.
    rc, txt = _run(["zypper", "--non-interactive", "--no-refresh", "--quiet",
                    "list-updates"], timeout=8)
    # Gate on rc only (like dnf/pacman). With --quiet a zero-update host prints
    # nothing and no "No updates found" banner, so the old `and txt` / banner
    # check left count=None ("needs elevated read") for an up-to-date SUSE host.
    if rc is not None:
        names = []
        for l in txt.splitlines():
            parts = [p.strip() for p in l.split("|")]
            if len(parts) >= 3 and parts[0] == "v":
                names.append(parts[2])
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("kernel-") for n in names)
    rc2, txt2 = _run(["zypper", "--non-interactive", "--no-refresh", "--quiet",
                      "list-patches", "--category", "security"], timeout=8)
    if rc2 is not None and txt2:
        out["security"] = sum(1 for l in txt2.splitlines()
                              if "security" in l.lower() and "|" in l)
    return out


def _dnf_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "dnf"}
    bin_ = "dnf" if _which("dnf") else "yum"
    # -C = cache-only (no network).
    if bin_ == "dnf":
        # repoquery emits one clean package name per line — unlike `check-update`,
        # which wraps a row across two lines when name.arch is wide (narrow,
        # non-tty output), dropping those packages from the count.
        rc, txt = _run(["dnf", "-C", "-q", "repoquery", "--upgrades", "--qf", "%{name}"],
                       timeout=10)
        if rc == 0:
            names = [l.strip() for l in txt.splitlines() if l.strip()]
            out["count"] = len(names)
            out["kernel"] = any(n.startswith("kernel") for n in names)
    else:
        # yum (RHEL7) has no builtin repoquery --upgrades; parse check-update but
        # reassemble wrapped rows (bare name.arch on one line, version/repo on the
        # next, indented). rc 100 = updates available, 0 = none.
        rc, txt = _run(["yum", "-C", "-q", "check-update"], timeout=10)
        if rc in (0, 100):
            names, pending = [], None
            for raw in txt.splitlines():
                if not raw.strip() or raw.startswith(("Obsoleting", "Last metadata", "Security:")):
                    continue
                parts = raw.split()
                if raw[:1].isspace() and pending:           # continuation: version repo
                    names.append(pending); pending = None
                elif len(parts) == 1 and "." in parts[0]:   # wrapped: name.arch only
                    pending = parts[0]
                elif len(parts) >= 3 and "." in parts[0]:   # name.arch version repo
                    names.append(parts[0]); pending = None
            out["count"] = len(names)
            out["kernel"] = any(n.startswith("kernel") for n in names)
    rc2, txt2 = _run([bin_, "-C", "-q", "updateinfo", "list", "security"], timeout=10)
    if rc2 == 0 and txt2:
        # Count distinct advisory IDs (first column), not package-instances — one
        # advisory fixing N packages would otherwise be counted N times.
        adv = set()
        for l in txt2.splitlines():
            f = l.split()
            if f and ("-" in f[0] or ":" in f[0]):   # FEDORA-2024-xxxx / RHSA-2024:xxxx
                adv.add(f[0])
        out["security"] = len(adv)
    return out


def _pacman_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "pacman"}
    # `pacman -Qu` compares against the cached sync DB — no refresh. Arch has no
    # security categorisation, so `security` stays None by design.
    rc, txt = _run(["pacman", "-Qu"], timeout=6)
    if rc is not None:
        names = [l.split()[0] for l in txt.splitlines() if l.strip()]
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("linux") for n in names)
    return out


def _apk_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "apk"}
    rc, txt = _run(["apk", "version", "-l", "<"], timeout=6)
    if rc is not None and txt:
        names = []
        for l in txt.splitlines():
            l = l.strip()
            if not l or l.startswith(("Installed", "WARNING")):
                continue
            names.append(l.split()[0])
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("linux-") for n in names)
    return out


def read_updates():
    try:
        if _which("apt") or _which("apt-get"):
            res = _apt_updates()
        elif _which("zypper"):
            res = _zypper_updates()
        elif _which("dnf") or _which("yum"):
            res = _dnf_updates()
        elif _which("pacman"):
            res = _pacman_updates()
        elif _which("apk"):
            res = _apk_updates()
        else:
            return None
    except Exception:
        return None
    if res is not None:
        res["checked"] = "cached"
    return res


def read_sec():
    return {"sec": {
        "firewall":        _firewall(),
        "ssh":             _ssh_config(),
        "selinux":         _selinux(),
        "apparmor":        _apparmor(),
        "fail2ban":        _fail2ban(),
        "reboot_required": _reboot_required(),
        "auto_updates":    _auto_updates(),
        "updates":         read_updates(),
    }}


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
            **read_os(),
            **read_hw(),
            **read_net(),
            **read_sec(),
            "disks": read_disks(),
            "hostname": socket.gethostname(),
        },
        "at": int(time.time()),
        "probe_version": "0.7",
    }
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
