"""Home shortcuts — the Overview launchpad's pinned apps.

One entry per pinned app: a name, the URL it opens, optionally the fleet host
and container it belongs to (which is where its status dot comes from), an
optional group label and an optional icon id. Stored as a JSON array in the
`home_shortcuts` setting, so it rides the existing settings backup/restore and
is shared by every browser that opens this hub — pin it on the laptop, see it
on the phone and on the wall screen.

Pure (no Flask, no DB) so the door validation is unit-testable, same discipline
as the custom AI servers registry next door in probes/.
"""
import json
import re

MAX_ENTRIES = 40
MAX_NAME = 40
MAX_URL = 500
MAX_GROUP = 24
MAX_HOST = 40
MAX_CONTAINER = 120

# Icon ids the dashboard can draw. Kept here, not in the browser, so a value
# that would render as nothing is rejected at the door instead of silently
# producing a blank tile. '' means "decide from the name" (brand logo, else a
# monogram) — the default, and what most pins will use.
ICONS = ("", "app", "media", "music", "film", "photo", "book", "game", "chat",
         "mail", "cloud", "code", "terminal", "database", "chart", "shield",
         "home", "network", "printer", "camera", "download", "ai", "tools",
         "calendar", "notes", "money", "cart", "heart", "globe")

# http/https only. A javascript:/data: href in a pinned tile would be stored
# XSS: the value comes back to every browser that opens this hub, and the tile
# is an <a href> the user clicks by design.
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def _clean_list(entries):
    """JSON-decoded value -> (clean list, error). An entry that is wrong is an
    error, never a silent drop: a shortcut that vanishes on save reads as "the
    app lost my pin" and there is no way for the user to find out why."""
    if not isinstance(entries, list):
        return None, "must be a JSON array"
    if len(entries) > MAX_ENTRIES:
        return None, f"at most {MAX_ENTRIES} shortcuts"
    out, seen = [], set()
    for e in entries:
        if not isinstance(e, dict):
            return None, "each shortcut must be an object"
        name = str(e.get("name") or "").strip()
        url = str(e.get("url") or "").strip()
        if not name:
            return None, "a shortcut needs a name"
        if len(name) > MAX_NAME:
            return None, f"'{name[:20]}…' name is longer than {MAX_NAME} characters"
        if len(url) > MAX_URL or not _URL_RE.match(url):
            return None, f"'{name}' needs a http:// or https:// address"
        icon = str(e.get("icon") or "").strip()
        if icon not in ICONS:
            return None, f"'{name}' has an unknown icon"
        group = str(e.get("group") or "").strip()[:MAX_GROUP]
        host = str(e.get("host") or "").strip()[:MAX_HOST]
        container = str(e.get("container") or "").strip()[:MAX_CONTAINER]
        key = (name.lower(), url.lower())
        if key in seen:
            return None, f"'{name}' is pinned twice"
        seen.add(key)
        out.append({"name": name, "url": url, "icon": icon, "group": group,
                    "host": host, "container": container})
    return out, None


def parse_shortcuts(raw):
    """The stored setting value -> (entries, error). Blank is the empty list,
    not an error — that is how the user clears every pin."""
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
    return _clean_list(entries)


def validate_shortcuts(raw):
    """Door validation for the settings POST: a user-facing error string, or
    None when the value is fine."""
    entries, err = parse_shortcuts(raw)
    return f"Home shortcuts: {err}." if err else None
