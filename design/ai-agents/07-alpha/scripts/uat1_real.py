"""UAT-1 re-check against the real model (vader qwen3.8-27b), one call at a time.
Each question must end with a real, concise answer in about 60 s.
  python uat1_real.py            # all, English and Bulgarian
  python uat1_real.py en         # English only
"""
import sys
import time

import hlm

c = hlm.Client()
c.login()
c.call("POST", "/api/agent/enable", {"enabled": True})
BASE = "http://100.76.27.18:18021/v1"
MODEL = "qwen3.8-27b"
pid = next((p["id"] for p in c.call("GET", "/api/agent/providers")[1]["providers"] if p["base_url"] == BASE), None)
if not pid:
    pid = c.call("POST", "/api/agent/providers", {"preset": "vllm", "base_url": BASE, "label": "vader qwen"})[1]["provider"]["id"]
c.call("PUT", "/api/agent/models", {"provider_id": pid, "model": MODEL, "context_window": 32768})
c.call("POST", "/api/agent/models/probe", {"provider_id": pid, "model": MODEL}, timeout=300)
c.call("PUT", "/api/agent/settings", {"default_provider_id": pid, "default_model": MODEL})

SETS = {
    "en": [["how are the disks across my fleet", "summarise"], ["what is using the most GPU"], ["anything wrong right now?"]],
    "bg": [["как са дисковете във флийта ми", "обобщи"], ["какво използва най-много GPU"], ["има ли проблем в момента?"]],
}
langs = sys.argv[1:] or ["en", "bg"]
for lang in langs:
    for convo in SETS[lang]:
        cid = c.new_conv()
        for q in convo:
            t0 = time.time()
            s, d = c.ask(cid, q)
            took = time.time() - t0
            if s != 202 and not isinstance(d, dict) or "run" not in (d or {}):
                print("!!", q, s, d)
                continue
            msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]
            run = d["run"]
            ans = [m["content"] for m in msgs["messages"] if m["role"] == "assistant" and m["run_id"] == run["id"] and m["content"]]
            tools = [s["tool"] for s in d["steps"] if s["kind"] in ("tool", "probe_result")]
            print("=" * 100)
            print("Q [%s]: %s | %s (%s) | %.0f s | %d model calls | tools: %s" % (
                lang, q, run["status"], run["status_reason"], took,
                sum(1 for s in d["steps"] if s["kind"] == "model"), ", ".join(tools) or "none"))
            print("A:", ans[-1] if ans else None)
