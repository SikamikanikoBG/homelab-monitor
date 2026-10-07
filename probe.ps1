<#
HomeLab Monitor - run-on-remote probe (Windows edition).

The hub pipes this file over SSH to a Windows host
(`ssh host powershell -NoProfile -NonInteractive -Command -`) every poll cycle.
Nothing persists on the remote: stdin is the script, stdout is one JSON blob.
Pure built-in cmdlets only (CIM / Get-Net* / Get-Service) - works on a stock
Windows 10/11 or Server box with Windows PowerShell 5.1, no install required.

The JSON shape is the SAME contract probe.py emits for Linux, so the hub's
All-hosts table and per-host System / Network / Security / Services tabs render
a Windows host through the exact same code paths. Fields that have no Windows
analogue (selinux, apparmor, load average, systemd) are simply omitted or
sent as a Windows-native equivalent; every section is best-effort and degrades
to a partial object rather than throwing, mirroring probe.py.
#>
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'
# Force invariant culture so numbers never serialize with a locale decimal comma
# (this box may be a non-US locale), and emit UTF-8 without a BOM so the hub's
# json.loads sees a clean leading '{'.
try { [System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}

function Epoch([datetime]$dt) {
    try { return [int64]($dt.ToUniversalTime() - [datetime]'1970-01-01T00:00:00Z').TotalSeconds }
    catch { return $null }
}

# ── CPU / memory / uptime ─────────────────────────────────────────────────────
function Read-CpuMemUptime {
    $out = @{}
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        $cs = Get-CimInstance Win32_ComputerSystem
        # LoadPercentage is an instantaneous per-socket sample; average across sockets.
        $load = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        if ($load -ne $null) { $out.cpu = [math]::Round([double]$load, 1) }
        $out.cores = [int]$cs.NumberOfLogicalProcessors
        if ($os.TotalVisibleMemorySize) {
            $out.ram_total = [int]([double]$os.TotalVisibleMemorySize / 1024)            # MB
            $out.ram_used  = [int](([double]$os.TotalVisibleMemorySize - [double]$os.FreePhysicalMemory) / 1024)
        }
        if ($os.LastBootUpTime) {
            $out.uptime = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
        }
    } catch {}
    return $out
}

# ── CPU temperature (best-effort; usually needs vendor WMI / admin) ────────────
function Read-Temp {
    try {
        $z = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
        $k = ($z | Measure-Object -Property CurrentTemperature -Average).Average   # tenths of Kelvin
        if ($k) {
            $c = [math]::Round(($k / 10.0) - 273.15, 1)
            if ($c -gt 0 -and $c -lt 130) { return @{ ctemp = $c } }
        }
    } catch {}
    return @{}
}

# ── GPU: nvidia-smi first, then a vendor-agnostic Windows fallback ─────────────
# NVIDIA cards report through nvidia-smi (same query probe.py uses). When that's
# absent we fall back to Windows' built-in GPU perf counters + WMI, which cover
# AMD and Intel GPUs (incl. integrated) with no vendor tool installed. The
# returned shape is identical either way, so the hub renders all three the same.
# Temperature/power aren't exposed by Windows for AMD/Intel without a vendor
# library, so they come back as 0 (the UI already tolerates missing fields).
function Read-Gpu {
    try {
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            $raw = & nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name --format=csv,noheader,nounits 2>$null
            $lines = @($raw | Where-Object { $_ -and $_.Trim() })
            if ($lines.Count -ge 1) {
                $p = $lines[0].Split(',') | ForEach-Object { $_.Trim() }
                if ($p.Count -ge 5) {
                    function I($v) { try { return [int][double]$v } catch { return 0 } }
                    # Per-card list (same shape as probe.py's read_gpu) so the
                    # System tab's Hardware card can name every card, not just GPU 0.
                    $gpus = @()
                    for ($i = 0; $i -lt $lines.Count; $i++) {
                        $q = $lines[$i].Split(',') | ForEach-Object { $_.Trim() }
                        if ($q.Count -ge 5) {
                            $gpus += [ordered]@{
                                idx = $i
                                name = $q[4]
                                mem_total = (I $q[1])
                                vendor = 'nvidia'
                            }
                        }
                    }
                    return @{
                        gpu = [ordered]@{
                            count     = $lines.Count
                            name      = $p[4]
                            mem_used  = (I $p[0])
                            mem_total = (I $p[1])
                            util      = (I $p[2])
                            temp      = (I $p[3])
                            vendor    = 'nvidia'
                        }
                        gpus = $gpus
                    }
                }
            }
        }
    } catch {}
    # ── Fallback: AMD / Intel (and NVIDIA if nvidia-smi is missing) via Windows ──
    try {
        # Only surface a real, recognised display GPU — skip Basic Display, virtual,
        # RDP and USB display adapters so headless servers don't show a phantom card.
        $cards = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'NVIDIA|GeForce|Quadro|Tesla|RTX|GTX|AMD|Radeon|Intel|Arc|Iris|UHD|HD Graphics' })
        if ($cards.Count -eq 0) { return @{} }
        $card = $cards | Sort-Object { [int64]($_.AdapterRAM) } -Descending | Select-Object -First 1
        $name = $card.Name

        # Utilisation: sum the engines within each engine type, take the busiest
        # type (this is what Task Manager shows as overall GPU %). 0 if unreadable.
        $util = 0
        try {
            $s = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples
            $perType = $s | Group-Object { ($_.InstanceName -replace '.*(engtype_[a-z0-9]+).*', '$1') } |
                ForEach-Object { ($_.Group | Measure-Object CookedValue -Sum).Sum }
            if ($perType) {
                $util = [int][math]::Round(($perType | Measure-Object -Maximum).Maximum)
                if ($util -lt 0) { $util = 0 } elseif ($util -gt 100) { $util = 100 }
            }
        } catch {}

        # VRAM used: dedicated GPU memory in use (MB). Shared memory is host RAM and
        # is already counted in the memory panel, so we don't fold it into "VRAM".
        $mem_used = 0
        try {
            $d = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples
            $mem_used = [int][math]::Round((($d | Measure-Object CookedValue -Sum).Sum) / 1MB)
        } catch {}

        # VRAM total: the adapter's dedicated memory size from the driver registry
        # (qwMemorySize is accurate where Win32_VideoController.AdapterRAM caps at 4 GB).
        $mem_total = 0
        try {
            $reg = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue |
                Where-Object { $_.DriverDesc -and $_.'HardwareInformation.qwMemorySize' -and ($_.DriverDesc -eq $name) } |
                Select-Object -First 1
            if (-not $reg) {
                $reg = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue |
                    Where-Object { $_.'HardwareInformation.qwMemorySize' } |
                    Sort-Object { [int64]$_.'HardwareInformation.qwMemorySize' } -Descending | Select-Object -First 1
            }
            if ($reg) { $mem_total = [int]([int64]$reg.'HardwareInformation.qwMemorySize' / 1MB) }
        } catch {}
        if ($mem_total -eq 0 -and $card.AdapterRAM) {
            try { $mem_total = [int]([int64]$card.AdapterRAM / 1MB) } catch {}
        }

        $vendor = if ($name -match 'NVIDIA|GeForce|Quadro|Tesla|RTX|GTX') { 'nvidia' }
                  elseif ($name -match 'AMD|Radeon') { 'amd' }
                  elseif ($name -match 'Intel|Arc|Iris|UHD|HD Graphics') { 'intel' }
                  else { 'unknown' }

        return @{ gpu = [ordered]@{
            count     = $cards.Count
            name      = $name
            mem_used  = $mem_used
            mem_total = $mem_total
            util      = $util
            temp      = 0
            vendor    = $vendor
        } }
    } catch { return @{} }
}

# ── Disks (fixed local volumes) ───────────────────────────────────────────────
function Read-Disks {
    $out = @()
    try {
        foreach ($d in Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3') {
            if (-not $d.Size -or [double]$d.Size -le 0) { continue }
            $total = [double]$d.Size
            $free  = [double]$d.FreeSpace
            $used  = $total - $free
            $out += [ordered]@{
                mount = "$($d.DeviceID)"
                total = [math]::Round($total / 1GB, 1)
                used  = [math]::Round($used  / 1GB, 1)
                pct   = [math]::Round($used * 100 / $total, 1)
            }
        }
    } catch {}
    return ,@($out | Sort-Object { $_.mount })
}

# ── OS / hardware inventory ───────────────────────────────────────────────────
function Read-OsHw {
    $os = @{}; $hw = @{}
    try {
        $o  = Get-CimInstance Win32_OperatingSystem
        $cs = Get-CimInstance Win32_ComputerSystem
        $cpu = @(Get-CimInstance Win32_Processor)
        $arch = $o.OSArchitecture
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        $os.id         = 'windows'
        $os.family     = 'windows'
        $os.pretty     = ($o.Caption | ForEach-Object { $_.Trim() })
        $os.version_id = "$($o.Version)"
        $os.kernel     = "$($o.Version)"                       # build number as the 'kernel' analogue
        $os.arch       = "$arch"
        $os.hostname   = $env:COMPUTERNAME
        $os.init       = 'windows-services'
        if ($cs.Domain) { $os.fqdn = "$($env:COMPUTERNAME).$($cs.Domain)".ToLower() }
        $os.boot_time  = Epoch $o.LastBootUpTime
        # Virtualization from the DMI model/manufacturer only. NOT HypervisorPresent:
        # that is true on bare metal whenever Hyper-V / WSL2 / VBS is enabled, so it
        # would mislabel a real laptop as a VM (it did, on the first pass).
        $blob = ("$($cs.Model) $($cs.Manufacturer)").ToLower()
        if     ($blob -match 'vmware')                            { $os.virt = 'vmware' }
        elseif ($blob -match 'virtualbox')                        { $os.virt = 'virtualbox' }
        elseif ($blob -match 'kvm|qemu')                          { $os.virt = 'kvm' }
        elseif ($blob -match 'xen')                               { $os.virt = 'xen' }
        elseif ($blob -match 'microsoft' -and $blob -match 'virtual') { $os.virt = 'hyper-v' }
        else                                                      { $os.virt = 'bare-metal' }
        $os.label = $os.pretty

        if ($cpu.Count -gt 0) {
            $hw.cpu_model   = "$($cpu[0].Name)".Trim()
            $hw.cpu_vendor  = "$($cpu[0].Manufacturer)".Trim()
            $hw.cpu_mhz_max = [int]$cpu[0].MaxClockSpeed
        }
        $hw.sockets = [int]$cs.NumberOfProcessors
        $hw.cores   = [int](($cpu | Measure-Object -Property NumberOfCores -Sum).Sum)
        $hw.threads = [int]$cs.NumberOfLogicalProcessors
        if ($o.TotalVisibleMemorySize) { $hw.ram_total = [int]([double]$o.TotalVisibleMemorySize / 1024) }
        $mfr = "$($cs.Manufacturer)".Trim(); $mdl = "$($cs.Model)".Trim()
        # Many OEMs repeat the vendor in the model ("HP HP ProBook…") — de-dupe it.
        if ($mdl -and $mfr -and $mdl.ToLower().StartsWith($mfr.ToLower())) { $machine = $mdl }
        else { $machine = (@($mfr, $mdl) | Where-Object { $_ }) -join ' ' }
        if ($machine.Trim()) { $hw.machine = $machine.Trim() }
    } catch {}
    return @{ os = $os; hw = $hw }
}

# ── Network ───────────────────────────────────────────────────────────────────
function Read-Net {
    $net = @{}
    try {
        $ifaces = @()
        $addrByIdx = @{}
        foreach ($a in (Get-NetIPAddress -ErrorAction SilentlyContinue)) {
            if (-not $addrByIdx.ContainsKey([int]$a.InterfaceIndex)) { $addrByIdx[[int]$a.InterfaceIndex] = @{ v4=@(); v6=@() } }
            if ($a.AddressFamily -eq 'IPv4') { $addrByIdx[[int]$a.InterfaceIndex].v4 += $a.IPAddress }
            elseif ("$($a.IPAddress)" -notmatch '^fe80') { $addrByIdx[[int]$a.InterfaceIndex].v6 += $a.IPAddress }
        }
        foreach ($ad in (Get-NetAdapter -ErrorAction SilentlyContinue | Sort-Object Name)) {
            $idx = [int]$ad.ifIndex
            $name = "$($ad.Name)"
            $t = 'ethernet'
            if     ($ad.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11') { $t = 'wifi' }
            elseif ($name -match 'Loopback')                                   { $t = 'loopback' }
            elseif ($ad.InterfaceDescription -match 'Hyper-V|Virtual|VMware|TAP|WSL') { $t = 'virtual' }
            elseif ($name -match 'WireGuard|wg')                               { $t = 'wireguard' }
            $iface = [ordered]@{
                name = $name
                type = $t
                ipv4 = @()
                ipv6 = @()
            }
            $mac = "$($ad.MacAddress)".Replace('-', ':').ToLower()
            if ($mac -and $mac -ne '00:00:00:00:00:00') { $iface.mac = $mac }
            if ($ad.Status) { $iface.state = "$($ad.Status)".ToLower() }
            if ($ad.MtuSize) { $iface.mtu = [int]$ad.MtuSize }
            try { $sp = [int64]$ad.LinkSpeed; if ($sp -gt 0) { $iface.speed_mbps = [int]($sp / 1MB) } } catch {}
            try {
                $st = Get-NetAdapterStatistics -Name $name -ErrorAction SilentlyContinue
                if ($st) { $iface.rx_bytes = [int64]$st.ReceivedBytes; $iface.tx_bytes = [int64]$st.SentBytes }
            } catch {}
            if ($addrByIdx.ContainsKey($idx)) { $iface.ipv4 = @($addrByIdx[$idx].v4); $iface.ipv6 = @($addrByIdx[$idx].v6) }
            $ifaces += $iface
        }
        $net.ifaces = $ifaces

        # default gateway + primary interface/IP
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                 Sort-Object RouteMetric | Select-Object -First 1
        if ($route) {
            if ($route.NextHop -and $route.NextHop -ne '0.0.0.0') { $net.gateway = "$($route.NextHop)" }
            $pidx = [int]$route.InterfaceIndex
            $pif  = $ifaces | Where-Object { $addrByIdx.ContainsKey($pidx) -and ($_.ipv4 -contains ($addrByIdx[$pidx].v4 | Select-Object -First 1)) } | Select-Object -First 1
            if ($pif) { $net.primary_iface = $pif.name }
            if ($addrByIdx.ContainsKey($pidx) -and $addrByIdx[$pidx].v4.Count -gt 0) { $net.primary_ip = $addrByIdx[$pidx].v4[0] }
        }
        # DNS servers (IPv4)
        $dns = @(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                 Select-Object -ExpandProperty ServerAddresses -ErrorAction SilentlyContinue |
                 Where-Object { $_ } | Select-Object -Unique)
        if ($dns.Count -gt 0) { $net.dns = $dns }
        if ($net.fqdn) {} elseif ($env:USERDNSDOMAIN) { $net.fqdn = "$($env:COMPUTERNAME).$($env:USERDNSDOMAIN)".ToLower() }

        # listening sockets + owning process; established count
        $listen = @()
        $procName = @{}
        try { foreach ($p in Get-Process) { $procName[[int]$p.Id] = $p.ProcessName } } catch {}
        foreach ($c in (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)) {
            $addr = "$($c.LocalAddress)"
            $exposed = ($addr -eq '0.0.0.0' -or $addr -eq '::')
            $row = [ordered]@{
                proto = 'tcp'; addr = $addr; port = [int]$c.LocalPort; exposed = [bool]$exposed
                proc  = $null
            }
            if ($procName.ContainsKey([int]$c.OwningProcess)) { $row.proc = $procName[[int]$c.OwningProcess] }
            $listen += $row
        }
        $net.listen = @($listen | Sort-Object @{e={-not $_.exposed}}, @{e={$_.port}})
        $net.established_count = @(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue).Count
    } catch {}
    return @{ net = $net }
}

# ── Security inventory (best-effort Windows analogues) ─────────────────────────
function Read-Sec {
    $sec = [ordered]@{
        firewall        = @{ backend = $null; active = $false }
        ssh             = $null
        selinux         = $null      # Linux-only -> UI shows neutral placeholder
        apparmor        = $null
        fail2ban        = @{ installed = $false }
        reboot_required = $false
        auto_updates    = $null
        updates         = $null
    }
    try {
        $profiles = Get-NetFirewallProfile -ErrorAction SilentlyContinue
        if ($profiles) {
            $on = @($profiles | Where-Object { $_.Enabled }).Count
            $sec.firewall = @{ backend = 'Windows Firewall'; active = [bool]($on -gt 0); open_ports = $null }
        }
    } catch {}
    # OpenSSH server config, if installed (ProgramData\ssh\sshd_config).
    try {
        $cfgPath = Join-Path $env:ProgramData 'ssh\sshd_config'
        if (Test-Path $cfgPath) {
            $ssh = @{}
            foreach ($ln in Get-Content $cfgPath) {
                $s = $ln.Trim()
                if (-not $s -or $s.StartsWith('#')) { continue }
                $parts = $s -split '\s+', 2
                if ($parts.Count -lt 2) { continue }
                switch ($parts[0].ToLower()) {
                    'permitrootlogin'        { if (-not $ssh.permit_root)   { $ssh.permit_root   = $parts[1].Split(' ')[0] } }
                    'passwordauthentication' { if (-not $ssh.password_auth) { $ssh.password_auth = $parts[1].Split(' ')[0] } }
                    'port'                   { try { if (-not $ssh.port) { $ssh.port = [int]$parts[1].Split(' ')[0] } } catch {} }
                }
            }
            if ($ssh.Count -gt 0) { $sec.ssh = $ssh }
        }
    } catch {}
    # reboot pending (Windows Update / CBS / pending file-rename).
    try {
        $pending = $false
        if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pending = $true }
        if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pending = $true }
        $sec.reboot_required = [bool]$pending
    } catch {}
    return @{ sec = $sec }
}

# ── Services (mapped into probe.py's systemd-shaped block so the Services tab
#    renders Windows services through the same renderer) ─────────────────────────
function Read-Services {
    try {
        $svcs = @(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue)
        if ($svcs.Count -eq 0) { return @{} }
        $loaded  = $svcs.Count
        $running = @($svcs | Where-Object { $_.State -eq 'Running' }).Count
        # "admin" units == Automatic-start services (the box's intended workloads),
        # analogous to /etc/systemd/system units. We do NOT report a "failed" count:
        # Windows has no equivalent state, and many Automatic services are
        # trigger/delayed-start and legitimately sit Stopped — calling those
        # "failed" (red) would be alarming and wrong. A stopped Automatic service is
        # shown amber ("stopped") instead, and the Failed KPI stays 0.
        $auto = @($svcs | Where-Object { $_.StartMode -eq 'Auto' })
        # Per-service RAM = its process working set. Many services share one
        # svchost.exe, so split a shared process's RAM evenly across the services
        # it hosts — otherwise each would claim the whole svchost and the Services
        # group would balloon far past the host's used RAM. Working set counts
        # shared DLLs, so it's approximate, but enough to see the heavy services.
        $ws = @{}
        foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) { $ws[[int]$p.Id] = [int64]$p.WorkingSet64 }
        $pidShare = @{}
        foreach ($s in $auto) {
            if ($s.State -eq 'Running' -and [int]$s.ProcessId -gt 0) {
                $k = [int]$s.ProcessId
                $pidShare[$k] = (([int]$pidShare[$k]) + 1)
            }
        }
        $rows = @()
        foreach ($s in ($auto | Sort-Object @{e={$_.State -eq 'Running'}}, Name)) {
            $isRun = $s.State -eq 'Running'
            $mem = $null
            $ppid = [int]$s.ProcessId
            if ($isRun -and $ppid -gt 0 -and $ws.ContainsKey($ppid)) {
                $share = [math]::Max(1, [int]$pidShare[$ppid])
                $mem = [int64]($ws[$ppid] / $share)
            }
            $row = [ordered]@{
                name      = "$($s.Name)"
                status    = $(if ($isRun) { 'ok' } else { 'warn' })
                active    = $(if ($isRun) { 'active' } else { 'inactive' })
                sub       = "$($s.State)".ToLower()
                desc      = "$($s.DisplayName)"
                admin     = $true
                watched   = $false
                ports     = @()
                uptime_s  = 0
                mem_bytes = $mem
            }
            $rows += $row
        }
        return @{ systemd = [ordered]@{
            available = $true
            summary   = @{ loaded = $loaded; running = $running; failed = 0; admin = $auto.Count }
            services  = $rows
        } }
    } catch { return @{} }
}

# ── AI models (Windows parity with probe.py's read_ai_models) ─────────────────
# Ollama on this host: the resident set from /api/ps (live VRAM) cross-referenced
# with the on-disk catalogue from /api/tags, so a model shows up whether or not it
# is currently loaded. Row shape matches probe.py exactly — the hub merges local
# and remote entries through the same code path. Read-only, 2 s timeouts, silent
# when ollama is not running here.
# ── Docker inventory (Containers tab) ────────────────────────────────────────
# Mirrors probe.py's read_docker(): `docker ps -a` for the list, one bounded
# `docker stats` pass for RAM/CPU%, a `docker ps -s` pass for writable-layer
# disk, and one `docker inspect` for the restart policy. Every call goes through
# Invoke-Docker, so a wedged daemon costs a column rather than the whole payload.
# The Docker CLI speaks
# the same `--format '{{json .}}'` contract on Windows as it does on Linux, so
# the JSON below is the exact shape the hub already renders for a Linux host —
# no hub-side branching. Docker Desktop's Linux-container mode answers all of
# it; a Windows-container daemon answers too, with Windows-native status
# strings. Read-only by design: the probe never starts or stops anything. {}
# when the CLI is absent or the daemon won't answer, matching probe.py — the
# Hosts-tab capability check is what tells the two apart.
function ConvertTo-Bytes([string]$v) {
    # '1.552GiB / 62.72GiB' (docker stats MemUsage) or '2.5MB (virtual 1.2GB)'
    # (docker ps -s Size) → the leading size in bytes, or $null. Same unit table
    # and same leading-number rule as probe.py's _MEM_UNITS / _stats_mem_bytes.
    if (-not $v) { return $null }
    $m = [regex]::Match($v, '^\s*([\d.]+)\s*([KMGT]i?B|B)', 'IgnoreCase')
    if (-not $m.Success) { return $null }
    $units = @{ 'b' = 1; 'kb' = 1000; 'kib' = 1024
                'mb' = 1000000; 'mib' = 1048576
                'gb' = 1000000000; 'gib' = 1073741824
                'tb' = 1000000000000; 'tib' = 1099511627776 }
    $mult = $units[$m.Groups[2].Value.ToLower()]
    if (-not $mult) { return $null }
    # Truncate, not round: probe.py does int(float(x) * mult), so a value like
    # 1.552GiB lands on the same byte as Linux does instead of one byte higher.
    try { return [int64][math]::Truncate([double]$m.Groups[1].Value * $mult) } catch { return $null }
}

# Runs the Docker CLI with a wall-clock cap.
#
# `& docker` has no timeout of its own, and the hub gives the whole payload only
# 15 s (`_ssh_with_stdin`) before it discards every section, not just this one.
# So each call below is bounded, and running past the cap degrades exactly like a
# non-zero exit: the caller keeps whatever the other passes produced.
#
# Returns @{ Ok = $true; Lines = @(...) }, or @{ Ok = $false; Lines = @() } when
# docker is absent, the daemon refuses, or the call outlives the cap.
#
# The two are kept apart deliberately: an empty list is a healthy daemon with no
# containers, while Ok = $false means "no Docker here". PowerShell treats $null
# and an empty array as equal, so a bare list could not tell those apart — every
# empty host would report a missing Docker instead.
function Invoke-Docker([string[]]$Arguments, [int]$TimeoutSec) {
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath 'docker' -ArgumentList $Arguments -NoNewWindow `
                           -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru
        if (-not $p.WaitForExit($TimeoutSec * 1000)) {
            try { $p.Kill() } catch { }
            return [pscustomobject]@{ Ok = $false; Lines = @() }
        }
        if ($p.ExitCode -ne 0) { return [pscustomobject]@{ Ok = $false; Lines = @() } }
        return [pscustomobject]@{ Ok = $true; Lines = @(Get-Content -LiteralPath $outFile -ErrorAction SilentlyContinue) }
    } catch {
        return [pscustomobject]@{ Ok = $false; Lines = @() }
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

# `docker ps --format '{{json .}}'` reports Names as a comma-separated list —
# the container name plus any --link aliases — and omits the key entirely for
# containers created before it existed. probe.py takes the first name and falls
# back to '?', so a container never reaches the hub with an empty name (which
# would render as a blank row). Keep the two sides in step.
function ConvertTo-ContainerName([string]$Names) {
    if (-not $Names) { return '?' }
    return ($Names -split ',')[0]
}

function Read-Docker {
    $out = @{}
    # 5 s: the list is the one pass whose failure means "no Docker here", so it
    # gets the tightest cap.
    $list = Invoke-Docker @('ps', '-a', '--no-trunc', '--format', '{{json .}}') 5
    if (-not $list.Ok) { return $out }
    $lines = $list.Lines

    $conts = @()
    foreach ($line in $lines) {
        $line = "$line".Trim()
        if (-not $line) { continue }
        try { $d = $line | ConvertFrom-Json } catch { continue }
        $state = "$($d.State)".ToLowerInvariant()
        $id    = "$($d.ID)"
        if ($id.Length -gt 64) { $id = $id.Substring(0, 64) }
        # RunningFor carries a trailing ' ago'; probe.py strips it. Only a
        # running container has a meaningful uptime — the rest stay empty.
        $uptime = ''
        # -creplace, not -replace: probe.py's replace(" ago", "") is
        # case-sensitive and drops every occurrence, not just a trailing one.
        if ($state -eq 'running') { $uptime = ("$($d.RunningFor)") -creplace ' ago', '' }
        $conts += [ordered]@{
            id     = $id
            name   = ConvertTo-ContainerName "$($d.Names)"
            image  = "$($d.Image)"
            state  = $state
            status = "$($d.Status)"
            ports  = "$($d.Ports)"
            uptime = $uptime
        }
    }

    # Restart policy — one `docker inspect` for the whole list, not one per
    # container. A failure here costs only that column.
    try {
        $ids = @($conts | ForEach-Object { $_.id } | Where-Object { $_ })
        if ($ids.Count -gt 0) {
            $pol = @{}
            $inspectArgs = @('inspect', '--format', "{{.Id}}`t{{.HostConfig.RestartPolicy.Name}}") + $ids
            foreach ($ln in (Invoke-Docker $inspectArgs 8).Lines) {
                $ln = "$ln"
                if (-not $ln.Trim()) { continue }
                $parts = $ln -split "`t", 2
                if ($parts.Count -lt 2) { continue }
                $cid = $parts[0].Trim()
                if ($cid.Length -gt 64) { $cid = $cid.Substring(0, 64) }
                $pname = $parts[1].Trim()
                if ($cid -and $pname) { $pol[$cid] = $pname }
            }
            foreach ($c in $conts) {
                if ($pol.ContainsKey($c.id)) { $c['restart_policy'] = $pol[$c.id] }
            }
        }
    } catch { }

    # Writable-layer disk per container. Sizes make the daemon walk layers, so
    # this pass is separate and its failure only costs the Disk column.
    try {
        $sizes = @{}
        foreach ($ln in (Invoke-Docker @('ps', '-a', '-s', '--no-trunc', '--format', "{{.ID}}`t{{.Size}}") 8).Lines) {
            $ln = "$ln"
            if (-not $ln.Trim()) { continue }
            $parts = $ln -split "`t", 2
            if ($parts.Count -lt 2) { continue }
            $cid = $parts[0].Trim()
            if ($cid.Length -gt 64) { $cid = $cid.Substring(0, 64) }
            $b = ConvertTo-Bytes $parts[1]
            if ($null -ne $b) { $sizes[$cid] = $b }
        }
        foreach ($c in $conts) {
            if ($sizes.ContainsKey($c.id)) { $c['disk_bytes'] = $sizes[$c.id] }
        }
    } catch { }

    $running = @($conts | Where-Object { $_.state -eq 'running' }).Count

    # One stats pass for RAM/CPU%. `docker stats` blocks ~1.5s to sample; the
    # cap in Invoke-Docker is what keeps a wedged daemon from sinking the whole
    # probe, so a timeout here degrades silently to a missing RAM column.
    if ($running -gt 0) {
        try {
            $stats = @{}
            foreach ($ln in (Invoke-Docker @('stats', '--no-stream', '--format', '{{json .}}') 8).Lines) {
                $ln = "$ln".Trim()
                if (-not $ln) { continue }
                try { $s = $ln | ConvertFrom-Json } catch { continue }
                if ($s.Name) { $stats["$($s.Name)"] = $s }
            }
            foreach ($c in $conts) {
                if (-not $stats.ContainsKey($c.name)) { continue }
                $s = $stats[$c.name]
                # RAM comes from docker-stats' MemUsage — the only memory figure
                # the Docker CLI exposes. probe.py moved to the Engine API's
                # anon+shmem derivation (which needs the daemon socket), but the
                # issue's "Docker CLI only, no SDK" contract rules that out here:
                # a stock Windows box doesn't hand PowerShell the named pipe. So
                # a container with a large mapped file reads higher here than it
                # would on Linux — the alternative was no RAM column at all.
                $mb = ConvertTo-Bytes "$($s.MemUsage)"
                if ($null -ne $mb) { $c['mem_bytes'] = $mb }
                $pct = "$($s.CPUPerc)".TrimEnd('%')
                if ($pct -match '^-?[\d.]+$') { $c['cpu_pct'] = [double]$pct }
            }
        } catch { }
    }

    # Same problem rule as probe.py: unhealthy, restarting, or a non-zero exit.
    $problems = 0
    foreach ($c in $conts) {
        # ToLowerInvariant, not ToLower: the latter folds through the thread's
        # culture, so on tr-TR "RESTARTING" becomes "restartıng" (dotless i).
        $st = "$($c.status)".ToLowerInvariant()
        if ($st -like '*unhealthy*') { $problems++ }
        elseif ($c.state -eq 'restarting') { $problems++ }
        # Case-sensitive on the raw status, matching probe.py's `"Exited (0)"
        # not in status`.
        elseif ($c.state -eq 'exited' -and "$($c.status)" -cnotmatch 'Exited \(0\)') { $problems++ }
    }

    $out['docker'] = @{
        available  = $true
        containers = @($conts)
        summary    = @{ total = $conts.Count; running = $running; problems = $problems }
    }
    return $out
}

function Read-OllamaModels {
    $api = 'http://127.0.0.1:11434'
    $host_name = $env:COMPUTERNAME

    # Resident set first: model name -> live VRAM in MB.
    $loaded = @{}
    try {
        $ps = Invoke-RestMethod -Uri "$api/api/ps" -TimeoutSec 2 -ErrorAction Stop
        foreach ($m in @($ps.models)) {
            if (-not $m.name) { continue }
            $vram = $null
            if ($m.size_vram) { $vram = [int][math]::Round($m.size_vram / 1MB) }
            $loaded[$m.name] = $vram
        }
    } catch { }

    # Full on-disk catalogue with registry detail. Note the real field names:
    # `name` already carries the tag, params/quant live under `details`, and the
    # timestamp is `modified_at`.
    $out = @()
    $known = @{}
    try {
        $tags = Invoke-RestMethod -Uri "$api/api/tags" -TimeoutSec 2 -ErrorAction Stop
        foreach ($m in @($tags.models)) {
            if (-not $m.name) { continue }
            $known[$m.name] = $true
            $isLoaded = $loaded.ContainsKey($m.name)
            $out += [ordered]@{
                host       = $host_name
                service    = 'ollama'
                provider   = 'ollama'
                model      = $m.name
                loaded     = $isLoaded
                vram_mb    = $(if ($isLoaded) { $loaded[$m.name] } else { $null })
                size_bytes = $m.size
                family     = $m.details.family
                param_size = $m.details.parameter_size
                quant      = $m.details.quantization_level
                modified   = $m.modified_at
            }
        }
    } catch { }

    # A model can be resident without being on disk (deleted while loaded, pulled
    # through another path) — keep it visible rather than letting it vanish.
    foreach ($name in $loaded.Keys) {
        if ($known.ContainsKey($name)) { continue }
        $out += [ordered]@{
            host = $host_name; service = 'ollama'; provider = 'ollama'
            model = $name; loaded = $true; vram_mb = $loaded[$name]
        }
    }
    # No unary comma here: the call site wraps this in @(...) which already
    # guarantees an array. `return ,$out` + @() double-wrapped it, so a host
    # with N models shipped [[N dicts]] and /api/models 500ed on the hub (#292).
    return $out
}

# ── Assemble + emit ───────────────────────────────────────────────────────────
# Merge helper-returned hashtables into one host block.
$merged = @{}
function Merge($h) { if ($h) { foreach ($k in $h.Keys) { $merged[$k] = $h[$k] } } }
Merge (Read-CpuMemUptime)
Merge (Read-Temp)
Merge (Read-Gpu)
$oshw = Read-OsHw
Merge @{ os = $oshw.os; hw = $oshw.hw }
Merge (Read-Net)
Merge (Read-Sec)
Merge (Read-Services)
Merge (Read-Docker)
$merged.disks    = (Read-Disks)
$merged.hostname = $env:COMPUTERNAME

# Model catalog sits alongside `host`, matching probe.py's payload shape.
$payload = [ordered]@{
    host          = $merged
    model_catalog = @(Read-OllamaModels)
    at            = (Epoch (Get-Date))
    probe_version = 'win-0.3'
}

# Single compact JSON line on stdout, nothing else.
$json = $payload | ConvertTo-Json -Depth 8 -Compress
[Console]::Out.Write($json)
[Console]::Out.Write("`n")
