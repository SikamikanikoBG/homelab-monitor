"""Probe functions — model-server discovery, host capability checks, uptime monitoring."""
import http.client
import json
import socket
import time
import urllib.error

# ── Group 1: Model-server probes (fully self-contained) ──────────────────────

def _http_json(ip, port, path, timeout=2):
    # try/finally so a down/slow model server (common: idle servers, wrong port
    # guesses) can't leak the TCP socket fd when request/read raises.
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        c.request("GET", path); r = c.getresponse(); body = r.read()
        status = r.status
    finally:
        c.close()
    return json.loads(body) if status < 400 else None

def probe_ollama(ip, port=11434):
    """Ollama: models loaded *now* from /api/ps — live VRAM plus any spill into
    system RAM (`size` is the whole resident model, `size_vram` the GPU part; the
    difference is CPU-offloaded — same split the Benchmark Lab calls ram_offload).
    A model with size_vram=0 but size>0 is running fully on CPU: still Loaded.
    Newer ollama also reports `context_length` — the num_ctx this load is running
    with, i.e. what sizes the KV cache — carried as a 4th element (None on older
    servers). If nothing is loaded, fall back to the pulled catalogue (/api/tags)
    so the server still shows as Idle. `port` defaults to the standard one; the
    custom-servers registry passes a user-chosen port instead."""
    ps = _http_json(ip, port, "/api/ps")
    loaded = []
    for m in (ps or {}).get("models", []):
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        if not m.get("name") or (size <= 0 and vram <= 0):
            continue
        ctx = m.get("context_length")
        loaded.append((m["name"], vram / 1048576, max(0, size - vram) / 1048576,
                       int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None))
    if loaded:
        return loaded
    tags = _http_json(ip, port, "/api/tags")
    return [(m["name"], None) for m in (tags or {}).get("models", []) if m.get("name")]

def _openai_models(*ports):
    """Factory for the OpenAI-compatible `GET /v1/models` shape (`data[].id`), shared by
    vLLM, llama.cpp/llama-server, LocalAI, faster-whisper-server/Speaches, koboldcpp,
    tabbyAPI, text-generation-webui, LM Studio, xinference, … — they differ only by port.
    Tries each candidate port until one answers with a non-empty model list. The
    `_openai` marker is what makes _OPENAI_KEYS (below) stay in sync with this
    factory automatically — a new provider added here is port-addressable for
    custom servers without a second edit."""
    def fn(ip):
        for p in ports:
            d = _http_json(ip, p, "/v1/models")
            data = (d or {}).get("data")
            if data:
                return [(m.get("id"), None) for m in data if m.get("id")]
        return []
    fn._openai = True
    return fn

def probe_tgi(ip):
    """HF Text-Generation-Inference / Text-Embeddings-Inference: `GET /info` → model_id."""
    d = _http_json(ip, 80, "/info") or _http_json(ip, 3000, "/info") or _http_json(ip, 8080, "/info")
    return [(d["model_id"], None)] if d and d.get("model_id") else []

def probe_koboldcpp(ip):
    d = _http_json(ip, 5001, "/api/v1/model")
    if d and d.get("result"):
        return [(d["result"], None)]
    return _openai_models(5001)(ip)

def probe_invokeai(ip):
    """InvokeAI v4+: `GET /api/v2/models/` → models[].name (its installed catalogue)."""
    d = _http_json(ip, 9090, "/api/v2/models/")
    return [(m.get("name"), None) for m in (d or {}).get("models", []) if m.get("name")]

def probe_a1111(ip):
    """AUTOMATIC1111 / SD.Next / Forge: the currently-loaded checkpoint. Kept to a single
    entry so the server's GPU VRAM (from nvidia-smi) attributes cleanly to it."""
    m = (_http_json(ip, 7860, "/sdapi/v1/options") or {}).get("sd_model_checkpoint")
    return [(m, None)] if m else []

def probe_whisper_asr(ip):
    """ahmetoner/whisper-asr-webservice (Whisper / WhisperX / faster-whisper
    engines). It has no model-list endpoint — just `/asr` — so we confirm it's up
    via `/openapi.json` and show one Idle entry; its GPU VRAM (nvidia-smi)
    attributes to it while it's transcribing. If something else answers here with
    the OpenAI shape, fall back to listing its models so we don't hide it."""
    for port in (9000, 8000):
        info = (_http_json(ip, port, "/openapi.json") or {}).get("info") or {}
        if "whisper" in (info.get("title", "") + " " + info.get("description", "")).lower():
            return [("Whisper ASR webservice", None)]
    return _openai_models(9000, 8000, 8080)(ip)

def probe_triton(ip):
    """NVIDIA Triton Inference Server: `GET /v2` returns server metadata. The live
    model list is a POST (/v2/repository/index), so we show a single Idle entry
    and let nvidia-smi VRAM attribute to it."""
    d = _http_json(ip, 8000, "/v2")
    if d and "triton" in (d.get("name", "") or "").lower():
        return [("Triton Inference Server", None)]
    return []

def probe_wyoming(ip):
    """Wyoming-protocol voice services (Home Assistant: wyoming-faster-whisper /
    -whisper ASR, -piper TTS, -openwakeword). Plain TCP + JSONL, not HTTP: send a
    `describe` event and read the `info` reply for the program/model names."""
    for port in (10300, 10200, 10400, 10500, 10700):
        try:
            with socket.create_connection((ip, port), timeout=2) as sk:
                sk.settimeout(2)
                sk.sendall(b'{"type": "describe"}\n')
                buf = b""
                while b"\n" not in buf and len(buf) < 65536:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                header, _, rest = buf.partition(b"\n")
                evt = json.loads(header.decode("utf-8", "replace") or "{}")
                if evt.get("type") != "info":
                    continue
                need = evt.get("data_length") or 0
                while len(rest) < need:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                data = json.loads(rest[:need].decode("utf-8", "replace")) if need else {}
        except Exception as e:
            print(f"probes/probe_wyoming error: {e}", flush=True)
            continue
        names = []
        for grp in ("asr", "tts", "wake", "handle", "intent"):
            for prog in (data.get(grp) or []):
                got = [m.get("name") for m in (prog.get("models") or []) if m.get("name")]
                names += got or ([prog["name"]] if prog.get("name") else [])
        return [(n, None) for n in names] or [("Wyoming service", None)]
    return []

def probe_comfy(ip):
    """ComfyUI: list installed checkpoints from /object_info (real model names). It has no
    'currently loaded' concept, so checkpoints show Idle and the server's GPU VRAM
    (nvidia-smi) attributes to the card. Falls back to a sentinel if it's up but bare."""
    if _http_json(ip, 8188, "/system_stats") is None:
        return []
    info = _http_json(ip, 8188, "/object_info/CheckpointLoaderSimple") or {}
    try:
        names = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        names = []
    return [(n, None) for n in names] or [("ComfyUI (no checkpoints)", None)]

# (key-substring → probe). The key is matched against the container's image AND name,
# first match wins, so order specific→generic. Most servers speak the OpenAI
# /v1/models shape and differ only by their default internal port.
PROBES = [
    ("ollama",                     probe_ollama),
    ("vllm",                       _openai_models(8000)),
    ("text-generation-inference",  probe_tgi),
    ("text-embeddings-inference",  probe_tgi),
    ("lorax",                      probe_tgi),
    # ASR / speech — keep the specific whisper-asr-webservice + WhisperX keys ahead
    # of the generic "whisper" so they don't fall through to the OpenAI probe.
    ("whisper-asr-webservice",     probe_whisper_asr),
    ("asr-webservice",             probe_whisper_asr),
    ("whisperx",                   probe_whisper_asr),
    ("whisper-asr",                probe_whisper_asr),
    ("faster-whisper",             _openai_models(8000)),
    ("speaches",                   _openai_models(8000)),
    ("whisper",                    _openai_models(8000, 9000)),
    ("wyoming",                    probe_wyoming),
    ("openedai-speech",            _openai_models(8000)),
    ("localai",                    _openai_models(8080)),
    ("local-ai",                   _openai_models(8080)),
    ("llama.cpp",                  _openai_models(8080, 8000)),
    ("llama-server",               _openai_models(8080, 8000)),
    ("llamacpp",                   _openai_models(8080, 8000)),
    ("ggml",                       _openai_models(8080, 8000)),
    ("koboldcpp",                  probe_koboldcpp),
    ("tabbyapi",                   _openai_models(5000)),
    ("exllama",                    _openai_models(5000)),
    ("text-generation-webui",      _openai_models(5000)),
    ("oobabooga",                  _openai_models(5000)),
    ("lmstudio",                   _openai_models(1234)),
    ("lm-studio",                  _openai_models(1234)),
    ("xinference",                 _openai_models(9997)),
    ("xorbits",                    _openai_models(9997)),
    ("aphrodite",                  _openai_models(2242)),
    ("mistral-rs",                 _openai_models(1234, 8080)),
    ("sglang",                     _openai_models(30000, 8000)),
    ("ramalama",                   _openai_models(8080, 8000)),
    ("nexa",                       _openai_models(8000)),
    ("openllm",                    _openai_models(3000, 8000)),
    ("litellm",                    _openai_models(4000)),
    ("gpustack",                   _openai_models(80, 8080)),
    ("cortex",                     _openai_models(39281, 1337)),
    ("janhq",                      _openai_models(1337)),
    ("triton",                     probe_triton),
    ("infinity",                   _openai_models(7997)),
    ("invokeai",                   probe_invokeai),
    ("invoke-ai",                  probe_invokeai),
    ("automatic1111",              probe_a1111),
    ("stable-diffusion-webui",     probe_a1111),
    ("sd-webui",                   probe_a1111),
    ("sdnext",                     probe_a1111),
    ("comfyui",                    probe_comfy),
]

# Providers whose probe is the _openai_models factory — i.e. the ones that only
# differ by port. Derived from the marker the factory sets, so adding a provider
# to PROBES keeps custom-server port targeting working without a second edit.
# (probe_whisper_asr borrows the OpenAI shape as a fallback but owns its own
# port ladder, so it is correctly absent from this set.)
_OPENAI_KEYS = {key for key, fn in PROBES if getattr(fn, "_openai", False)}

def _match_probe(ct):
    """Return the probe fn for a container whose image/name matches a known server, else None."""
    img, name = ct.get("image", "").lower(), ct.get("name", "").lower()
    for key, fn in PROBES:
        if key in img or key in name:
            return fn
    return None

def _match_probe_key(ct):
    """Return the PROBES key (provider id, e.g. 'ollama', 'vllm', 'lmstudio') for a
    container whose image/name matches a known server, else None. Same match order
    as _match_probe — kept as a separate lookup so callers that only need the
    provider label don't have to carry the fn around."""
    img, name = ct.get("image", "").lower(), ct.get("name", "").lower()
    for key, fn in PROBES:
        if key in img or key in name:
            return key
    return None

CATALOG_MAX = 15   # max idle "available" models listed per server before collapsing to a count

def _normalize_model_rows(raw, label):
    """Normalize a probe's raw rows to (model, vram_mb, ram_spill_mb, ctx_now).
    Only Ollama reports the spill split + runtime context today, so narrower
    rows get None (= unknown) for the missing fields. Shared by probe_models and
    probe_custom_server so the two discovery paths can't drift. Also applies the
    oversized-idle-catalogue collapse: loaded models and small catalogues (e.g.
    your pulled Ollama models) are kept verbatim, an oversized one (faster-whisper
    exposes its full upstream registry of 400+ models) becomes a single summary
    row so it can't flood the panel."""
    found = [(it[0], it[1], it[2] if len(it) > 2 else None, it[3] if len(it) > 3 else None)
             for it in raw if it[0]]
    loaded = [x for x in found if x[1] is not None]
    idle   = [x for x in found if x[1] is None]
    if len(idle) > CATALOG_MAX:
        idle = [(f"{len(idle)} models available", None, None, None)]
    return loaded + idle

def probe_models(ct):
    fn = _match_probe(ct)
    if not fn:
        return []
    # Host-networked servers have no per-container IP; the hub shares the host net
    # namespace, so localhost reaches them on their published/default port.
    ip = ct.get("ip") or "127.0.0.1"
    try:
        return _normalize_model_rows(fn(ip), ct.get("name", ""))
    except Exception as e:
        print(f"probes/probe_models error: {e}", flush=True)
        return []

def _openai_models_at(ip, port):
    """OpenAI-compatible GET /v1/models on ONE explicit port (vLLM, llama.cpp,
    LocalAI, LM Studio, …). Used by the custom-servers registry, where the user
    has already said which port to talk to — no candidate-port guessing."""
    data = (_http_json(ip, port, "/v1/models") or {}).get("data")
    return [(m.get("id"), None) for m in (data or []) if isinstance(m, dict) and m.get("id")]

def _probe_for_provider(provider, ip, port):
    """Resolve a PROBES provider key to its probe fn at a user-chosen port.
    Every standard probe takes (ip, ...) and ignores an explicit port, so the
    port-aware ones (ollama, the OpenAI family) are special-cased and the rest
    run on their built-in default ports."""
    fn = dict(PROBES).get(provider)
    if not fn:
        return []
    if provider == "ollama":
        return probe_ollama(ip, port)
    if provider in _OPENAI_KEYS:
        return _openai_models_at(ip, port)
    # Remaining providers carry their own port logic; a custom port doesn't
    # apply to them (there is nothing to override), so run them on defaults.
    return fn(ip)

def probe_custom_server(desc):
    """A user-registered AI server from the custom_ai_servers setting:
    {"name","host","port","provider"} — the auto-discovery only covers the hub's
    containers on standard ports and remotes' localhost ollama, so a vLLM living
    on another box at a non-standard port needs this to show up. Provider is a
    PROBES key; the port is always the user's explicit one. Normalizes rows the
    same way probe_models does, so the sampler's catalog merge is shape-agnostic."""
    # Two descriptor shapes reach here: the settings door / Test endpoint send
    # "host", the sampler reuses the container-descriptor shape and sends "ip".
    # Accept both, or the sampler silently probes an empty host and the user's
    # registered server never shows up while its Test button still passes.
    ip = (desc.get("host") or desc.get("ip") or "").strip()
    provider = (desc.get("provider") or "").strip()
    try:
        port = int(desc.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not ip or not (1 <= port <= 65535) or not provider:
        return []
    try:
        return _normalize_model_rows(_probe_for_provider(provider, ip, port),
                                     desc.get("name", provider))
    except Exception as e:
        print(f"probes/probe_custom_server error ({desc.get('name')}): {e}", flush=True)
        return []

def parse_custom_servers(raw):
    """Parse + clean the custom_ai_servers setting value (a JSON array string) into
    [{"name","host","port","provider","fleet_host"}]. Pure — unit-testable.
    Returns (entries, error): a non-None error means the value is malformed and
    must be rejected, not silently dropped. Blank input is the empty list, not an
    error — that is how the user clears the setting. Pre-fleet_host stored values
    parse fine: a missing key becomes "" (= the hub)."""
    if raw is None:
        return [], None
    if not isinstance(raw, str):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            return None, "must be a JSON array"
    raw = raw.strip()
    if not raw:
        return [], None
    try:
        entries = json.loads(raw)
    except ValueError:
        return None, "not a JSON array"
    return _clean_custom_server_list(entries)

def _clean_custom_server_list(entries):
    """The shared validation body: a JSON-decoded value → (clean list, error).
    A non-list, an oversized list, or a bad entry each produces an error, so a
    malformed value is rejected rather than silently truncated. Entries are
    {name, host, port, provider, fleet_host}: fleet_host is the fleet name the
    server runs on ("" or "local" = the hub) — where its models show up on the
    AI Models tab. The hub still probes it; only the stamping follows the name."""
    if not isinstance(entries, list):
        return None, "must be a JSON array"
    if len(entries) > 20:
        return None, "at most 20 entries"
    out = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict):
            return None, "each entry must be an object"
        name = str(e.get("name") or "").strip()
        host = str(e.get("host") or "").strip()
        provider = str(e.get("provider") or "").strip()
        try:
            port = int(e.get("port"))
        except (TypeError, ValueError):
            return None, f"'{name or '?'}' has a bad port"
        if not name or not host or not (1 <= port <= 65535) or provider not in dict(PROBES):
            return None, f"'{name or '?'}' needs name, host, a port and a known provider"
        fleet_host = e.get("fleet_host")
        if fleet_host is None:
            fleet_host = ""
        elif not isinstance(fleet_host, str):
            return None, f"'{name or '?'}' fleet_host must be a host name"
        fleet_host = fleet_host.strip()
        if len(fleet_host) > 40:
            return None, f"'{name or '?'}' fleet_host is longer than 40 characters"
        if (name, host, port) in seen:
            return None, f"'{name}' at {host}:{port} is listed twice"
        seen.add((name, host, port))
        out.append({"name": name, "host": host, "port": port, "provider": provider,
                    "fleet_host": fleet_host})
    return out, None

def validate_custom_servers(raw):
    """Door validation for the settings POST: returns a user-facing error string
    if the value is malformed, else None. Pure (no DB / no PROBES mutation) so it
    is unit-testable. A non-string value (a direct API client posting the list
    itself) is coerced the same way the route stores it, then validated — never
    crash on .strip()."""
    entries_err = None
    if raw is None:
        return None
    if not isinstance(raw, str):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            return "Custom AI servers must be a JSON array."
    raw = raw.strip()
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        return "Custom AI servers must be a JSON array, e.g. [{\"name\":\"vllm\",\"host\":\"vader\",\"port\":8010,\"provider\":\"vllm\"}]."
    _clean, err = _clean_custom_server_list(entries)
    return f"Custom AI servers: {err}." if err else None

# ── Group 2: Host metrics probe ───────────────────────────────────────────────

def probe_host_metrics(user, host, port, family="linux", timeout=15):
    """Run the right probe on the remote via SSH stdin. Returns
    (data, error, elapsed_ms, timed_out). Windows hosts get the PowerShell probe;
    everything else gets probe.py — both emit the same JSON shape, so the caller
    and the UI don't branch on OS. `timed_out` is True only when ssh hit the
    timeout (rc 124), which is what the adaptive calibration keys off (#99)."""
    import app as _app
    if family == "windows":
        if not _app._PROBE_PS_SCRIPT:
            return None, "probe.ps1 not packaged in this image", 0, False
        rc, out, err, ms = _app._ssh_with_stdin(user, host, port, _app._WIN_PS_CMD,
                                                _app._PROBE_PS_SCRIPT, timeout=timeout)
    else:
        if not _app._PROBE_SCRIPT:
            return None, "probe.py not packaged in this image", 0, False
        rc, out, err, ms = _app._ssh_with_stdin(user, host, port, "python3 -",
                                                _app._PROBE_SCRIPT, timeout=timeout)
    if rc != 0:
        return None, _app._clean_ssh_err(err, out, rc), ms, rc == 124
    # lstrip a UTF-8 BOM: PowerShell over SSH can prepend one, which str.strip()
    # won't remove and which would otherwise make json.loads choke on char 0.
    out = (out or "").lstrip("﻿").strip()
    if not out:
        return None, "empty response from probe", ms, False
    try:
        return json.loads(out), None, ms, False
    except Exception as e:
        print(f"probes/_run_probe bad JSON: {e}", flush=True)
        return None, f"bad JSON from probe: {e}", ms, False

# ── Group 3: probe_host ───────────────────────────────────────────────────────

def probe_host(name):
    """Run the capability checklist against a registered host. Each check returns
    {id, label, status: ok|warn|fail|info, detail, remedy?}. The full result is
    cached on the host row so the UI can show the last-known state even when
    the user hasn't re-tested."""
    import app as _app
    from backend.db.repos import hosts as hosts_repo
    with _app.LOCK:
        ssh_target = hosts_repo.get_ssh_target(name, conn=_app.DB)
    if not ssh_target:
        return None
    row = (ssh_target,)
    parsed = _app._parse_ssh_target(ssh_target)
    if not parsed:
        result = {"checks": [{"id": "parse", "label": "SSH target", "status": "fail",
                              "detail": f"could not parse '{row[0]}'"}]}
        result["summary"] = _app._summarize(result["checks"])
        _app._record_check(name, result)
        return result
    user, host, port = parsed
    _app._ensure_ssh_keypair()
    checks = []
    os_info = {}

    # 1) SSH connect.  Pre-probe TCP-22 so we can give a clear "port closed"
    # answer instead of letting ssh time out cryptically.
    tcp_ok, tcp_err = _app._tcp_probe(host, port)
    if not tcp_ok:
        item = {"id": "connect", "label": "SSH reachable", "status": "fail",
                "detail": f"port {port} not reachable: {tcp_err}",
                "debug": tcp_err,
                "remedy": _app._remedy_sshd_check()}
        checks.append(item)
        result = {"checks": checks, "summary": _app._summarize(checks), "os": {}}
        _app._record_check(name, result)
        return result

    rc, out, err, ms = _app._ssh(user, host, port, "echo ok", timeout=_app.SSH_CONNECT_TIMEOUT + 2)
    if rc == 0 and out == "ok":
        checks.append({"id": "connect", "label": "SSH reachable",
                       "status": "ok", "detail": f"port {port}, {ms} ms"})
    else:
        msg = _app._clean_ssh_err(err, out, rc)
        hint = None
        e = (err or "")
        if "Permission denied" in e:
            hint = _app._remedy_pubkey(user)
        elif "Host key verification failed" in e:
            hint = {"where": "on the hub (this container)",
                    "cmd": f"# Clear the saved host key and re-test:\nrm {_app.SSH_KNOWN_HOSTS}"}
        elif "Could not resolve hostname" in e or "Name or service not known" in e:
            hint = {"where": "on the hub (this container)",
                    "cmd": "# Hostname did not resolve. Try the IP, or check the container's DNS."}
        elif rc == 124:
            hint = _app._remedy_sshd_down(None)
        item = {"id": "connect", "label": "SSH reachable", "status": "fail",
                "detail": msg, "debug": (err or "")[:2000]}
        if hint: item["remedy"] = hint
        checks.append(item)
        result = {"checks": checks, "summary": _app._summarize(checks), "os": {}}
        _app._record_check(name, result)
        return result

    # SSH connected — detect the remote OS so the rest of the checklist can
    # produce remedies that match the actual distro (and so the UI can show an
    # OS badge per host).
    os_info = _app._detect_os(user, host, port)
    if (os_info.get("family") or "") == "macos":
        # Be honest about limitations: macOS doesn't expose /proc, doesn't have
        # systemd, doesn't expose the Docker socket the same way, and has no
        # nvidia-smi. We still record the OS but mark the rest as info.
        checks.append({"id": "os", "label": "Detected OS", "status": "info",
                       "detail": os_info.get("label") or "macOS"})
        checks.append({"id": "proc",   "label": "/proc readable",   "status": "info",
                       "detail": "Linux-only — macOS doesn't expose /proc"})
        checks.append({"id": "docker", "label": "Docker socket",    "status": "info",
                       "detail": "macOS Docker socket flow not supported in v0"})
        checks.append({"id": "dbus",   "label": "systemd D-Bus",    "status": "info",
                       "detail": "macOS uses launchd, not systemd"})
        checks.append({"id": "nvidia", "label": "nvidia-smi",       "status": "info",
                       "detail": "no NVIDIA driver path on macOS"})
        result = {"checks": checks, "summary": _app._summarize(checks), "os": os_info}
        _app._record_check(name, result)
        return result

    if (os_info.get("family") or "") == "windows":
        # Windows host: same checklist, Windows-native checks. All run through
        # PowerShell-over-stdin so cmd.exe quoting can't bite us. The probe is the
        # PowerShell one (probe.ps1); these checks just gate it and drive the UI.
        checks.append({"id": "os", "label": "Detected OS", "status": "ok",
                       "detail": os_info.get("label") or "Windows"})
        # /proc analogue: can we read host state via WMI/CIM?
        _, out, err, _ = _app._ssh_with_stdin(user, host, port, _app._WIN_PS_CMD,
            b"\"up=$([int]((Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds)\"",
            timeout=10)
        if out and "up=" in out:
            checks.append({"id": "proc", "label": "Host readable (WMI)", "status": "ok",
                           "detail": f"uptime {out.strip().split('up=')[-1][:12]}s"})
        else:
            checks.append({"id": "proc", "label": "Host readable (WMI)", "status": "warn",
                           "detail": (err or "no reply from PowerShell")[:160]})
        # Docker on Windows: first check the CLI is even on PATH — only THEN run
        # it. Previously we ran `docker version` straight away and treated any
        # failure (missing CLI *or* a daemon-side error) as "not installed".
        # But Docker Desktop's client/engine API-version handshake can itself
        # fail with a message like "request returned 500 Internal Server Error
        # ... check if the server supports the requested API version" — that
        # text contains the word "error", so it used to get misclassified as
        # "CLI not on PATH" even though Docker is very much installed, just
        # unhealthy. Split "CLI absent" (info) from "CLI present but the
        # daemon didn't respond cleanly" (warn) like the Linux path already
        # does for permission-denied vs. not-installed.
        _, out, err, _ = _app._ssh_with_stdin(user, host, port, _app._WIN_PS_CMD,
            b"if(-not (Get-Command docker -EA SilentlyContinue)){Write-Output 'DOCKER_ABSENT'}else{"
            b"$v=(docker version --format '{{.Server.Version}}' 2>&1);"
            b"if($LASTEXITCODE -eq 0){Write-Output \"DOCKER_OK:$v\"}else{Write-Output \"DOCKER_ERR:$v\"}}",
            timeout=12)
        line = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if line.startswith("DOCKER_OK:") and "." in line:
            checks.append({"id": "docker", "label": "Docker", "status": "ok",
                           "detail": f"server v{line.split(':', 1)[1].strip()}"})
        elif line.startswith("DOCKER_ERR:"):
            checks.append({"id": "docker", "label": "Docker", "status": "warn",
                           "detail": f"Docker is installed on {name} but the daemon didn't respond cleanly: "
                                     f"{line.split(':', 1)[1].strip()[:160]}",
                           "remedy": _app._remedy_docker_windows_daemon()})
        else:
            checks.append({"id": "docker", "label": "Docker", "status": "info",
                           "detail": "Docker CLI not found on PATH — container panel hidden for this host"})
        # systemd D-Bus analogue: Windows services are always queryable.
        checks.append({"id": "dbus", "label": "Windows services", "status": "ok",
                       "detail": "Get-Service available"})
        # nvidia-smi works on Windows too when the driver is installed.
        _, out, _, _ = _app._ssh_with_stdin(user, host, port, _app._WIN_PS_CMD,
            b"if(Get-Command nvidia-smi -EA SilentlyContinue){(nvidia-smi --query-gpu=name --format=csv,noheader -i 0)}else{'missing'}",
            timeout=10)
        nv = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if nv and nv != "missing":
            checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "ok", "detail": nv[:120]})
        else:
            checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "info",
                           "detail": "not found — GPU panel will be hidden for this host"})
        result = {"checks": checks, "summary": _app._summarize(checks), "os": os_info}
        _app._record_check(name, result)
        return result

    if os_info.get("label"):
        checks.append({"id": "os", "label": "Detected OS", "status": "ok",
                       "detail": os_info["label"]})

    # 2) /proc readable
    rc, out, err, _ = _app._ssh(user, host, port, "head -n1 /proc/uptime 2>&1")
    if rc == 0 and out:
        try:
            up = float(out.split()[0])
            detail = f"uptime {int(up)}s"
        except Exception:
            detail = out[:60]
        checks.append({"id": "proc", "label": "/proc readable", "status": "ok", "detail": detail})
    else:
        checks.append({"id": "proc", "label": "/proc readable", "status": "warn",
                       "detail": (err or "unexpected reply")[:160]})

    # 3) Docker socket
    rc, out, err, _ = _app._ssh(user, host, port,
                           "docker version --format '{{.Server.Version}}' 2>&1 || true")
    out_l = (out or "").lower()
    err_l = (err or "").lower()
    if rc == 0 and out and not any(s in out_l for s in
                                   ("permission denied", "cannot connect", "error", "command not found")):
        checks.append({"id": "docker", "label": "Docker socket", "status": "ok",
                       "detail": f"server v{out}"})
    elif "permission denied" in out_l or "permission denied" in err_l:
        checks.append({"id": "docker", "label": "Docker socket", "status": "warn",
                       "detail": f"permission denied — '{user}' not in the docker group",
                       "remedy": _app._remedy_docker_group(user, os_info)})
    elif "command not found" in out_l or "command not found" in err_l or out_l == "":
        checks.append({"id": "docker", "label": "Docker socket", "status": "info",
                       "detail": "Docker not installed — container panel will be hidden for this host"})
    else:
        checks.append({"id": "docker", "label": "Docker socket", "status": "warn",
                       "detail": (out or err or "unknown error")[:160]})

    # 4) systemd D-Bus
    _, out, _, _ = _app._ssh(user, host, port,
                        "[ -S /run/dbus/system_bus_socket ] && echo ok || echo missing")
    if out == "ok":
        checks.append({"id": "dbus", "label": "systemd D-Bus", "status": "ok",
                       "detail": "/run/dbus/system_bus_socket present"})
    else:
        checks.append({"id": "dbus", "label": "systemd D-Bus", "status": "info",
                       "detail": "not present — services panel will be hidden for this host"})

    # 5) GPU — NVIDIA via nvidia-smi, else AMD via the amdgpu sysfs interface (no ROCm).
    _, out, _, _ = _app._ssh(user, host, port,
                        "command -v nvidia-smi >/dev/null && "
                        "nvidia-smi --query-gpu=name --format=csv,noheader -i 0 2>/dev/null | head -1 || echo missing")
    if out and out != "missing":
        checks.append({"id": "nvidia", "label": "GPU (NVIDIA)", "status": "ok", "detail": out[:120]})
    else:
        # No NVIDIA — look for an AMD card (PCI vendor 0x1002) in /sys/class/drm.
        _, amd, _, _ = _app._ssh(user, host, port,
                        "for c in /sys/class/drm/card*/device; do "
                        "if [ \"$(cat $c/vendor 2>/dev/null)\" = 0x1002 ]; then "
                        "cat $c/product_name 2>/dev/null || echo 'AMD GPU'; break; fi; done")
        if amd:
            checks.append({"id": "nvidia", "label": "GPU (AMD)", "status": "ok",
                           "detail": ("%s · amdgpu sysfs" % amd[:110])})
        else:
            checks.append({"id": "nvidia", "label": "GPU", "status": "info",
                           "detail": "no NVIDIA or AMD GPU found — GPU panel will be hidden for this host"})

    result = {"checks": checks, "summary": _app._summarize(checks), "os": os_info}
    _app._record_check(name, result)
    return result

# ── Group 4: probe_http and probe_tcp ────────────────────────────────────────

def probe_http(target, timeout, expected=None):
    """GET the URL, following ≤ _UPTIME_MAX_REDIRECTS redirects. Returns
    (up, latency_ms, code, err). up = connected AND status matches expected (or any
    2xx/3xx if expected unset). Never raises; bounded by `timeout`. The error string
    is redacted of any embedded credentials before it leaves here."""
    import app as _app
    start = time.monotonic()
    try:
        try:
            code = _app._http_probe_once(target, timeout, "GET")
        except urllib.error.HTTPError as he:
            code = he.code            # a 4xx/5xx still answered — that's a real status
        latency = round((time.monotonic() - start) * 1000, 1)
        if expected is not None:
            up = (code == expected)
        else:
            up = (200 <= code < 400)
        return up, latency, code, (None if up else f"HTTP {code}")
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return False, latency, None, _app._redact_target(str(e))[:200]

def probe_tcp(target, timeout):
    """socket.create_connection to host:port, bounded by `timeout`. up = connects.
    Returns (up, latency_ms, None, err). Never raises."""
    import app as _app
    start = time.monotonic()
    host, port = _app._parse_host_port(target)
    if host is None:
        return False, None, None, "bad host:port"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = round((time.monotonic() - start) * 1000, 1)
            return True, latency, None, None
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return False, latency, None, _app._redact_target(str(e))[:200]
