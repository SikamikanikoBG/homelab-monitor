"""The tools a run is offered, and how a call to one is answered.

- Read tools: the shared registry (backend/tools/read.py), scoped by host
  policy — an Off host can't be read at all; on fleet-wide tools its rows are
  removed by their structured host field before the model sees them.
- Probes: read-only catalogue checks (`risk: none`), as `<id with __>` tools.
- Actions: catalogue actions, offered only when at least one host in scope
  could ask/allow them; the server still decides every call.

Results go back to the model wrapped as data (`untrusted_data`), capped in size.
"""
import json

from backend.agent import catalog, policy, ports, state, store
from backend.agent.guards import cap_result
from backend.tools import validate, defaults
from backend.tools.read import BY_NAME as READ, READ_TOOLS, client

# Which field of a fleet-wide result row names its host.
_ROW_HOST_KEY = {"list_hosts": "name"}

REASON_PARAM = {"type": "string", "maxLength": 300,
                "description": "One sentence: why this is needed (shown to the owner)."}


def install_transport():
    client().set_transport(ports.internal_get)


class Scope:
    """Everything a run's tool layer needs to know, read once per model step."""

    def __init__(self, hosts=None, run_mode="act", task_actions=None, task_tools=None, controls=None):
        all_hosts = ports.host_names()
        self.hosts = [h for h in (hosts or all_hosts) if h in all_hosts]
        self.modes = store.host_modes()
        self.rules = store.action_rules()
        self.run_mode = run_mode
        self.task_actions = task_actions
        self.task_tools = task_tools
        self.controls = ports.controls_enabled() if controls is None else controls

    def mode(self, host):
        return policy.mode_for(host, self.modes)

    def visible(self, host):
        return host in self.hosts and self.mode(host) != "off"

    def off_hosts(self):
        return {h for h in ports.host_names() if self.mode(h) == "off"} | \
               {h for h in ports.host_names() if h not in self.hosts}


def _host_param(hosts):
    return {"type": "string", "enum": sorted(hosts), "description": "Which host."}


def specs(scope):
    """[{name, description, parameters}] for the model, plus a lookup for dispatch."""
    out, kinds = [], {}
    for t in READ_TOOLS:
        if scope.task_tools is not None and t.name not in scope.task_tools:
            continue
        if t.host_scope == "hub" and not scope.visible("local"):
            continue
        params = json.loads(json.dumps(t.params))
        if t.host_scope in ("arg", "host_opt"):
            vis = [h for h in scope.hosts if scope.visible(h)]
            if not vis:
                continue
            params["properties"][t.host_arg] = dict(params["properties"][t.host_arg], enum=sorted(vis))
        out.append({"name": t.name, "description": t.description, "parameters": params})
        kinds[t.name] = ("read", t.name)
    for a in catalog.actions():
        probe = catalog.is_probe(a)
        if not probe and scope.run_mode == "report":
            continue
        if scope.task_actions is not None and a["id"] not in scope.task_actions:
            if not probe or (scope.task_tools is not None and a["id"] not in scope.task_tools):
                continue
        eligible = []
        for h in scope.hosts:
            if not scope.visible(h) or not catalog.supported(a, h, policy.os_family(h)) \
                    or not policy.requires_met(a, h):
                continue
            if not probe:
                if not scope.controls or scope.mode(h) != "active":
                    continue
                if policy.rule_for(h, a["id"], scope.rules)[0] not in ("ask", "allow"):
                    continue
            eligible.append(h)
        if not eligible:
            continue
        params = json.loads(json.dumps(a["params"]))
        params.setdefault("properties", {})
        params["properties"] = dict({"host": _host_param(eligible)}, **params["properties"])
        params["required"] = ["host"] + list(params.get("required") or [])
        if not probe:
            params["properties"]["reason"] = REASON_PARAM
        desc = a["title"] + (" (read-only check run on the host over SSH; slower than the hub's data — use "
                             "it when list_hosts/get_host can't answer)" if probe else " — risk: %s" % a["risk"])
        name = catalog.tool_name(a["id"])
        out.append({"name": name, "description": desc, "parameters": params})
        kinds[name] = ("probe" if probe else "action", a["id"])
    return out, kinds


def _filter_rows(obj, off, key):
    if isinstance(obj, list):
        return [_filter_rows(x, off, key) for x in obj
                if not (isinstance(x, dict) and x.get(key) in off)]
    if isinstance(obj, dict):
        return {k: _filter_rows(v, off, key) for k, v in obj.items()}
    return obj


def call_read(scope, name, args):
    """(ok, text) for a read tool call."""
    spec = READ[name]
    errs = validate(spec.params, args)
    if errs:
        return False, "invalid arguments: " + "; ".join(errs)
    args = defaults(spec.params, args)
    if spec.host_scope in ("arg", "host_opt"):
        host = args.get(spec.host_arg) or "local"
        if host not in ports.host_names():
            return False, "error: there is no host called %s. list_hosts shows the hosts." % str(host)[:60]
        if not scope.visible(host):
            return False, "not permitted: %s is Off for the agent (or not part of this task)." % host
    if spec.host_scope == "hub" and not scope.visible("local"):
        return False, "not permitted: the hub is Off for the agent."
    install_transport()
    try:
        data = spec.handler(**args)
    except Exception as e:                       # MonitorError and friends: tell the model, it can adapt
        print("agent tool %s failed: %s" % (name, e), flush=True)
        return False, "error: %s" % str(e)[:300]
    if spec.host_scope == "fleet":
        data = _filter_rows(data, scope.off_hosts(), _ROW_HOST_KEY.get(name, "host"))
    return True, wrap(name, data)


def wrap(name, data):
    text = json.dumps({"tool": name, "untrusted_data": data}, ensure_ascii=False, default=str)
    return cap_result(text)
