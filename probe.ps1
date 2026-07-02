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

# ── GPU via nvidia-smi (identical query to probe.py) ──────────────────────────
function Read-Gpu {
    try {
        if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return @{} }
        $raw = & nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name --format=csv,noheader,nounits 2>$null
        $lines = @($raw | Where-Object { $_ -and $_.Trim() })
        if ($lines.Count -eq 0) { return @{} }
        $p = $lines[0].Split(',') | ForEach-Object { $_.Trim() }
        if ($p.Count -lt 5) { return @{} }
        function I($v) { try { return [int][double]$v } catch { return 0 } }
        return @{ gpu = [ordered]@{
            count     = $lines.Count
            name      = $p[4]
            mem_used  = (I $p[0])
            mem_total = (I $p[1])
            util      = (I $p[2])
            temp      = (I $p[3])
        } }
    } catch { return @{} }
}

# ── Disk I/O (throughput / utilisation / latency via perf counters) ───────────
# Mirrors the hub's collect_disk_io() JSON shape so remote Windows hosts report
# disk I/O too. Read Bytes/sec + Write Bytes/sec → MB/s; % Disk Time → util%;
# Avg. Disk sec/Read|Write (seconds) → ms/op. Degrades to {} if Get-Counter or
# the PhysicalDisk counter set is unavailable, so nothing else breaks.
function Read-DiskIo {
    try {
        if (-not (Get-Command Get-Counter -ErrorAction SilentlyContinue)) { return @{} }
        $paths = @(
            '\PhysicalDisk(*)\Disk Read Bytes/sec',
            '\PhysicalDisk(*)\Disk Write Bytes/sec',
            '\PhysicalDisk(*)\% Disk Time',
            '\PhysicalDisk(*)\Avg. Disk sec/Read',
            '\PhysicalDisk(*)\Avg. Disk sec/Write'
        )
        $samp = Get-Counter -Counter $paths -ErrorAction Stop
        $by = @{}
        foreach ($s in $samp.CounterSamples) {
            $inst = "$($s.InstanceName)"
            if (-not $inst -or $inst -eq '_total') { continue }
            if (-not $by.ContainsKey($inst)) {
                $by[$inst] = [ordered]@{ device = $inst; read_mb_s = 0.0; write_mb_s = 0.0
                                         util_pct = $null; read_lat_ms = $null; write_lat_ms = $null }
            }
            $p = "$($s.Path)"
            $v = [double]$s.CookedValue
            if     ($p -match 'read bytes/sec')  { $by[$inst].read_mb_s  = [math]::Round($v / 1e6, 1) }
            elseif ($p -match 'write bytes/sec') { $by[$inst].write_mb_s = [math]::Round($v / 1e6, 1) }
            elseif ($p -match '% disk time')     { $by[$inst].util_pct   = [math]::Round([math]::Min(100.0, [math]::Max(0.0, $v)), 1) }
            elseif ($p -match 'sec/read')        { $by[$inst].read_lat_ms  = [math]::Round($v * 1000, 2) }
            elseif ($p -match 'sec/write')       { $by[$inst].write_lat_ms = [math]::Round($v * 1000, 2) }
        }
        $items = @($by.Values | Sort-Object { -($_.read_mb_s + $_.write_mb_s) })
        if ($items.Count -eq 0) { return @{} }
        $tr = 0.0; $tw = 0.0
        foreach ($i in $items) { $tr += $i.read_mb_s; $tw += $i.write_mb_s }
        return @{ disk_io = [ordered]@{
            available = $true
            summary   = @{ total_read_mb_s = [math]::Round($tr, 1); total_write_mb_s = [math]::Round($tw, 1) }
            items     = ,@($items)
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
Merge (Read-DiskIo)
$merged.disks    = (Read-Disks)
$merged.hostname = $env:COMPUTERNAME

$payload = [ordered]@{
    host          = $merged
    at            = (Epoch (Get-Date))
    probe_version = 'win-0.1'
}

# Single compact JSON line on stdout, nothing else.
$json = $payload | ConvertTo-Json -Depth 8 -Compress
[Console]::Out.Write($json)
[Console]::Out.Write("`n")
