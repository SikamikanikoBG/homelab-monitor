"""backend/api/icons_api.py — app icons for containers and services.

One route the browser points an `<img>` at. It takes the container or unit name
(and its image or description as a hint), resolves that to a selfh.st icon slug
against the catalogue the hub keeps in memory, and serves the SVG from the hub's
own cache — fetching it once, the first time anyone asks. A name with no icon
gets a 404 the browser is told to remember, so an unrecognised container costs
one request, not one per page load.

See backend/icons.py for why the browser never talks to the CDN itself.
"""
from flask import Blueprint, request, jsonify, Response

import backend.icons as icons

bp = Blueprint('icons_api', __name__)

# An icon never changes under its slug, so let the browser keep it. A miss is
# remembered for an hour only — a container the catalogue doesn't know today may
# well be in next week's index refresh.
HIT_CACHE  = "public, max-age=604800"
MISS_CACHE = "public, max-age=3600"


def _store():
    import app as _app
    return getattr(_app, "ICON_STORE", None)


def _miss():
    return Response("", status=404, headers={"Cache-Control": MISS_CACHE})


@bp.route("/api/icon")
def api_icon():
    """?name=<container|unit>&ref=<image|description> → image/svg+xml, or 404."""
    store = _store()
    if store is None:
        return _miss()
    name = (request.args.get("name") or "")[:200]
    ref  = (request.args.get("ref") or "")[:300]
    slug = (request.args.get("slug") or "").strip().lower()
    if not slug:
        if not name:
            return _miss()
        store.refresh_index_async()          # weekly catalogue refresh, off-thread
        slug = store.index.resolve(name, ref)
    if not slug or not icons.SLUG_RE.match(slug):
        return _miss()
    body = store.get(slug)
    if not body:
        return _miss()
    # Served to an <img>, where an SVG cannot run script — and locked down anyway,
    # because this file came off the internet and the hub is now vouching for it.
    return Response(body, mimetype="image/svg+xml", headers={
        "Cache-Control": HIT_CACHE,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    })


@bp.route("/api/icons/status")
def api_icons_status():
    """What the Setup card shows: how many icons the hub knows, how many it holds."""
    store = _store()
    if store is None:
        return jsonify({"enabled": False, "known": 0, "cached": 0, "fetched_at": None})
    import app as _app
    out = store.stats()
    out["enabled"] = _app.get_settings().get("icon_pack", "1") == "1"
    return jsonify(out)
