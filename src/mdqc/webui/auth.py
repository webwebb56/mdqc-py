"""Web UI auth helpers.

The token-validation middleware is owned by Agent E (registered on the FastAPI
app at construction time). This module only provides the small helpers the web
UI needs to convert the `?token=...` query string into an `mdqc_session` cookie
and strip the token from the URL bar on first page load.
"""

from __future__ import annotations

SESSION_COOKIE_NAME = "mdqc_session"


def make_session_cookie(token: str) -> str:
    """Build a Set-Cookie header value for the session cookie.

    HttpOnly, SameSite=Lax, Path=/, no Expires (session cookie). Not Secure
    because the web UI is loopback-http only.
    """
    return f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax"


__all__ = ["SESSION_COOKIE_NAME", "make_session_cookie"]
