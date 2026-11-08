"""Off host must be invisible to every read tool (US-4: 'cannot even read it')."""
import hlm, json
from t_policy import setp, c
setp("lab2", "off")
tools = [("list_hosts", {}), ("get_host", {"name": "lab2"}), ("get_host", {"host": "lab2"}), ("get_containers", {"host": "lab2"}),
         ("get_containers", {}), ("get_snapshot", {}), ("get_services", {}), ("get_memory", {}), ("get_history", {}),
         ("get_events", {}), ("get_alerts", {}), ("get_uptime", {}), ("get_costs", {}), ("get_gpu", {}), ("get_installed_models", {}),
         ("get_ai_models", {}), ("docker__system_df", {"host": "lab2"}), ("host__disk_usage", {"host": "lab2"}), ("scan_disk", {"path": "/", "host": "lab2"})]
for t, a in tools:
    cid = c.new_conv()
    s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": t, "arguments": a}]}, {"text": "ok"}]))
    msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
    said = " ".join(str(m.get("content")) for m in msgs if m["role"] == "tool")
    print(f"{t:22s} {json.dumps(a):28s} mentions lab2={'lab2' in said} hlm-target2={'hlm-target2' in said} len={len(said)} :: {said[:160]}")
setp("lab2", "passive")
