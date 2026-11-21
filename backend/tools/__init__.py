"""One tool registry for the MCP server and the in-app agent (#328).

A tool is declared once, as data: name, a short description, a JSON-schema
for its parameters, and the function that answers it. The read tools live in
`read.py` (the same functions the MCP server exposes); the agent adds its
catalogue actions on top in backend/agent/toolbox.py.

`validate()` is a small JSON-schema subset (type, enum, pattern, min/max,
minLength/maxLength, required, additionalProperties, items) — enough for
every schema we declare, with no dependency.
"""
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "object": dict, "array": list}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict
    handler: Callable = field(compare=False)
    kind: str = "read"                 # read | probe | action
    host_scope: str = "fleet"          # arg | hub | fleet | host_opt
    host_arg: Optional[str] = None     # the param naming the host, when host_scope is arg/host_opt
    agent_only_params: tuple = ()      # params the agent has that the MCP tool doesn't

    def public(self):
        return {"name": self.name, "description": self.description, "parameters": self.params,
                "kind": self.kind, "host_scope": self.host_scope}


def validate(schema, value, path="args"):
    """Return a list of human-readable problems; empty means valid."""
    errs = []
    t = schema.get("type")
    if t:
        want = _TYPES[t]
        ok = isinstance(value, want) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return ["%s must be %s" % (path, t)]
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s must be one of %s" % (path, ", ".join(map(str, schema["enum"]))))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append("%s is too short" % path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append("%s is too long (max %d)" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errs.append("%s has characters that aren't allowed" % path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s must be at least %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("%s must be at most %s" % (path, schema["maximum"]))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errs.append("%s.%s is required" % (path, req))
        for k, v in value.items():
            if k in props:
                errs.extend(validate(props[k], v, "%s.%s" % (path, k)))
            elif schema.get("additionalProperties") is False:
                errs.append("%s.%s is not a known parameter" % (path, k))
    if isinstance(value, list) and "items" in schema:
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s has too many items" % path)
        for i, v in enumerate(value):
            errs.extend(validate(schema["items"], v, "%s[%d]" % (path, i)))
    return errs


def defaults(schema, args):
    """args with schema defaults filled in."""
    out = dict(args or {})
    for k, p in (schema.get("properties") or {}).items():
        if k not in out and "default" in p:
            out[k] = p["default"]
    return out
