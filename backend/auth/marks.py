"""Access marks a route declares about itself, read by the auth gate.

A route says what it is where it is defined, so the gate needs no list of
paths: `@open_access` (reachable without a login: health check, metrics,
public status page, the login page itself), `@sensitive` (refused to read-only
tokens even for GET: the database backup, key listings, provider settings),
and `push_ok` (an experiments push route a `push`-scope key may call).

Each decorator returns the same function with an attribute set, so it can sit
under `@bp.route` without changing anything else about the view.
"""


def open_access(fn):
    fn._hlm_open = True
    return fn


def open_unless_env(name):
    """Open, unless the named env var is set to 1 (e.g. METRICS_REQUIRE_AUTH)."""
    def deco(fn):
        fn._hlm_open = name
        return fn
    return deco


def sensitive(fn):
    fn._hlm_sensitive = True
    return fn


def push_ok(fn):
    fn._hlm_push_ok = True
    return fn


def is_open(view):
    import os
    mark = getattr(view, "_hlm_open", False)
    if isinstance(mark, str):
        return (os.environ.get(mark) or "").strip().lower() not in ("1", "true", "yes", "on")
    return bool(mark)


def is_sensitive(view):
    return bool(getattr(view, "_hlm_sensitive", False))


def is_push_ok(view):
    return bool(getattr(view, "_hlm_push_ok", False))
