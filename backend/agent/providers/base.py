"""What every model adapter speaks.

Messages inside the agent are OpenAI-shaped dicts:
  {"role": "user"|"assistant"|"tool", "content": str|None,
   "tool_calls": [{"id", "name", "arguments": dict}],   # assistant only
   "tool_call_id": str, "name": str,                     # tool only
   "provider_meta": dict|None}                           # opaque, echoed back (e.g. Gemini signatures)
Each adapter translates to and from its provider's wire format.
"""
from dataclasses import dataclass, field
from typing import Optional

ERROR_KINDS = ("auth", "unreachable", "rate_limited", "server", "bad_request", "context",
               "timeout", "protocol", "cancelled")

# Which kinds are worth a retry — and only before the first byte arrives.
RETRYABLE = ("unreachable", "rate_limited", "server", "timeout")

ERROR_TEXT = {
    "auth": "the provider rejected the key (401/403)",
    "unreachable": "the provider can't be reached",
    "rate_limited": "the provider is rate-limiting us (429)",
    "server": "the provider had an internal error (5xx)",
    "bad_request": "the provider refused the request (400)",
    "context": "the conversation no longer fits the model's context window",
    "timeout": "the provider took too long to answer",
    "protocol": "the provider sent something we couldn't read",
    "cancelled": "stopped",
}


class ProviderError(Exception):
    def __init__(self, kind, message="", status=None, retry_after=None):
        self.kind = kind if kind in ERROR_KINDS else "protocol"
        self.status = status
        self.retry_after = retry_after
        self.message = (message or "")[:300]
        super().__init__("%s: %s" % (self.kind, self.message))

    def human(self):
        base = ERROR_TEXT.get(self.kind, self.kind)
        return base + (" — " + self.message if self.message else "")


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    estimated: bool = False


@dataclass
class ChatRequest:
    model: str
    system: str
    messages: list
    tools: list = field(default_factory=list)   # [{"name","description","parameters"}]
    max_tokens: int = 2048
    temperature: float = 0.2
    num_ctx: Optional[int] = None
    no_tools: bool = False     # answer in text: the tools are described (history needs them) but not callable


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [{"id","name","arguments"}]
    finish: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    ttft_ms: Optional[int] = None
    ms: int = 0
    provider_meta: Optional[dict] = None


@dataclass
class ModelInfo:
    id: str
    context_window: Optional[int] = None
    tools_hint: Optional[str] = None      # 'yes' | 'no' | None (unknown)


def parse_args(raw):
    """Tool-call arguments as a dict; malformed JSON is kept as {'__raw__': text}
    so the loop can tell the model exactly what was wrong (Jarvis2 port)."""
    import json
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {"__raw__": str(raw)[:2000]}
    return v if isinstance(v, dict) else {"__raw__": str(raw)[:2000]}


def estimate_tokens(text_chars):
    """When a server returns no usage, estimate from characters (marked ~)."""
    return int(text_chars / 3.2)
