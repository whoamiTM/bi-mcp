"""Typed exceptions for the Blue Iris MCP server.

Each exception carries a ``kind`` (a stable string discriminator) and a ``hint``
(a one-line human-readable remediation). The MCP layer turns these into
structured ``{error, kind, hint}`` responses for Claude.
"""

from __future__ import annotations


class BiError(Exception):
    """Base class — Blue Iris returned an unexpected/unspecified error."""

    kind: str = "bi_error"
    hint: str = "Blue Iris reported an error. Check the BI status window for details."

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint

    def to_dict(self) -> dict:
        return {"error": str(self), "kind": self.kind, "hint": self.hint}


class BiUnreachable(BiError):
    kind = "unreachable"
    hint = (
        "Cannot reach Blue Iris. Check BI_HOST and BI_PORT in .env, "
        "and that Blue Iris's web server is enabled (Settings > Web server)."
    )


class BiAuthFailed(BiError):
    kind = "auth"
    hint = (
        "Blue Iris rejected the login. Check BI_USER and BI_PASS in .env. "
        "Note: Blue Iris locks accounts after repeated failed logins."
    )


class BiAdminAuthFailed(BiAuthFailed):
    """Variant of BiAuthFailed for the optional admin user — points operators
    at BI_ADMIN_USER/BI_ADMIN_PASS instead of the read-user creds."""

    kind = "admin_auth"
    hint = (
        "Blue Iris rejected the admin login. Check BI_ADMIN_USER and "
        "BI_ADMIN_PASS in .env. Note: Blue Iris locks accounts after repeated "
        "failed logins."
    )


class BiNotFound(BiError):
    kind = "not_found"
    hint = "The requested camera, clip, or alert does not exist on this Blue Iris install."


class BiBadRequest(BiError):
    kind = "bad_request"
    hint = "The arguments to this tool were malformed. Check the tool's documented parameters."


class BiAdminRequired(BiError):
    """The requested cmd is gated behind admin BI credentials, but the server
    is configured with only the read-only user. Distinct from BiAuthFailed
    (which is "I tried to log in and BI said no") and from a generic BiError
    "Access denied" (which is BI's own wording mid-session)."""

    kind = "admin_required"
    hint = (
        "This tool requires admin Blue Iris credentials. Set BI_ADMIN_USER "
        "and BI_ADMIN_PASS in bi-mcp/.env to a BI user with admin enabled."
    )
