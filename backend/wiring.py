"""Everything the login and the AI agent add to the Flask app, in one call.

app.py calls `install(app)` once, next to its other blueprints, so the feature
costs app.py one line. Both halves are dormant by default: the gate is a no-op
until an admin password is set, and every /api/agent/* route answers 404 unless
AGENT_ENABLED=1.
"""
import os


def install(app):
    from backend.api.auth_api import bp as auth_bp
    from backend.api.agent_api import bp as agent_bp
    from backend.auth import gate
    app.register_blueprint(auth_bp)
    app.register_blueprint(agent_bp)
    gate.install(app)


def start_workers():
    """Boot-time work once the database is open: seed the admin password from
    ADMIN_PASSWORD(_FILE) if one is given and none is set, then start the
    agent's threads — only when it is switched on (see backend/agent/boot.py)."""
    from backend.auth import sessions
    from backend.agent import boot, state
    if os.environ.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD_FILE"):
        sessions.seed_from_env()
    if state.env_allowed():
        boot.start_if_enabled()
