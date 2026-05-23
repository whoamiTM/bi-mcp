"""Typed exceptions + structured error envelopes for the Blue Iris MCP server.

Two layers:

  * **Typed exceptions** (``BiError`` and subclasses): raised inside the dispatch
    functions. Each carries a stable ``kind`` discriminator and a one-line
    ``hint``. ``server.py`` converts them to JSON for Claude.

  * **ErrorCode + create_error_response()**: structured codes and a
    suggestion table modeled on ha-mcp's ``errors.py``. Used when a tool
    wants to return a *non-exceptional* failure payload (e.g. a batch
    item failed). New code should prefer raising typed exceptions; this
    table exists so the canonical suggestions live in one place and stay
    in sync between exceptions and explicit failure payloads.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions (existing surface, kept for back-compat)
# ---------------------------------------------------------------------------


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
    kind = "admin_required"
    hint = (
        "This tool requires admin Blue Iris credentials. Set BI_ADMIN_USER "
        "and BI_ADMIN_PASS in bi-mcp/.env to a BI user with admin enabled."
    )


class BiMutationsDisabled(BiError):
    """A mutating tool was invoked while ``BI_MCP_ALLOW_MUTATIONS`` is unset.

    In practice the registry will skip mutation modules entirely when the flag
    is off, so this should never surface — but it exists as a defensive guard
    in case a caller bypasses the registry (e.g. direct CLI use of a tool
    function).
    """

    kind = "mutations_disabled"
    hint = (
        "Mutating tools (bi_trigger_camera, bi_set_ptz_preset, bi_set_profile, "
        "bi_export_clip, bi_update_record) are disabled by default. Set "
        "BI_MCP_ALLOW_MUTATIONS=1 in bi-mcp/.env to enable them. Read "
        "AGENTS.md § Mutation patterns first."
    )


class BiVerifyInconclusive(BiError):
    """Raised by verify-after-write helpers when the post-write read could not
    complete — e.g. the throwaway admin login transiently failed, or the
    fresh client could not reach BI.

    Dispatchers should CATCH this rather than letting it propagate: the write
    itself already succeeded (verify only runs after a successful write
    reply), so the correct outcome is a structured "changed-but-unverified"
    response, not a hard auth/error to the caller. Surfacing this as a
    typed exception keeps the verify helpers ignorant of the dispatcher's
    response shape while letting the dispatcher distinguish verify-side
    blips from real verify mismatches (which are still ``BiError``).

    The two concrete subclasses below — ``BiVerifyAuthBlip`` and
    ``BiVerifyUnreachable`` — carry distinct ``kind`` values so callers can
    escalate auth-class blips (which may indicate creds rotation, lockout,
    or BI restart) differently from network-class blips. The base class is
    retained for ``except BiVerifyInconclusive`` catch-alls in dispatchers.
    """

    kind = "verify_inconclusive"
    hint = (
        "The write was accepted by Blue Iris but the post-write verify read "
        "could not be completed (transient auth or connectivity blip). The "
        "change may or may not have landed — re-read the field to confirm."
    )


class BiVerifyAuthBlip(BiVerifyInconclusive):
    """Verify-side throwaway login could not authenticate.

    Causes range from transient (BI session pressure, brief auth hiccup,
    parallel admin logins) to durable (creds rotated, account locked out).
    Callers seeing this repeatedly across multiple tool calls should
    investigate ``BI_ADMIN_USER``/``BI_ADMIN_PASS`` rather than treat each
    occurrence as transient.
    """

    kind = "verify_auth_blip"
    hint = (
        "The write was accepted, but the verify-side admin login failed. "
        "If this recurs, check BI_ADMIN_USER/BI_ADMIN_PASS for rotation or "
        "lockout — Blue Iris locks accounts after repeated failed logins."
    )


class BiVerifyUnreachable(BiVerifyInconclusive):
    """Verify-side fresh client could not reach Blue Iris.

    Network blip, timeout, or BI restarted between the write and the verify
    read. Almost always transient.
    """

    kind = "verify_unreachable"
    hint = (
        "The write was accepted, but the verify-side read could not reach "
        "Blue Iris (network blip or BI restart). Re-read the field to "
        "confirm the change landed."
    )


class BiStaleReg(BiError):
    """A .reg file used by bi_get_reg is older than the staleness threshold.

    Surfaced as a *warning* in the tool's normal response shape (not raised)
    in the common case. The exception variant exists for callers that want
    to treat staleness as a hard failure.
    """

    kind = "stale_reg"
    hint = (
        "The .reg export under bi-mcp's cam settings/ is older than the "
        "freshness threshold. Re-export the camera (right-click camera in "
        "Blue Iris → Camera settings → Copy/import → Export) before quoting "
        "values from it."
    )


# ---------------------------------------------------------------------------
# Structured error codes (ha-mcp pattern, adapted)
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Stable string codes for structured error responses.

    These overlap deliberately with ``BiError.kind`` so a code can be derived
    from any raised exception. Adding a new code? Update
    ``DEFAULT_SUGGESTIONS`` below in the same change.
    """

    # Connection / auth
    BI_UNREACHABLE = "BI_UNREACHABLE"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_FAILED = "AUTH_FAILED"
    ADMIN_REQUIRED = "ADMIN_REQUIRED"
    ADMIN_AUTH_FAILED = "ADMIN_AUTH_FAILED"

    # Resource lookup
    CAMERA_NOT_FOUND = "CAMERA_NOT_FOUND"
    ALERT_NOT_FOUND = "ALERT_NOT_FOUND"
    CLIP_NOT_FOUND = "CLIP_NOT_FOUND"

    # Request shape
    VALIDATION_FAILED = "VALIDATION_FAILED"

    # Mutation gating
    MUTATIONS_DISABLED = "MUTATIONS_DISABLED"

    # PTZ / mutation outcomes
    PTZ_FAILED = "PTZ_FAILED"
    TRIGGER_FAILED = "TRIGGER_FAILED"
    PROFILE_SET_FAILED = "PROFILE_SET_FAILED"

    # .reg parsing
    STALE_REG = "STALE_REG"
    REG_PARSE_FAILED = "REG_PARSE_FAILED"

    # Catch-all
    BI_ERROR = "BI_ERROR"


DEFAULT_SUGGESTIONS: dict[ErrorCode, str] = {
    ErrorCode.BI_UNREACHABLE: (
        "Check BI_HOST/BI_PORT in .env and confirm Blue Iris's web server is enabled "
        "(Settings → Web server)."
    ),
    ErrorCode.AUTH_EXPIRED: "Re-run the tool — the client will re-login automatically.",
    ErrorCode.AUTH_FAILED: (
        "Check BI_USER/BI_PASS in .env. Blue Iris locks accounts after repeated failed logins."
    ),
    ErrorCode.ADMIN_REQUIRED: (
        "Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env to a BI user with admin enabled."
    ),
    ErrorCode.ADMIN_AUTH_FAILED: (
        "Check BI_ADMIN_USER/BI_ADMIN_PASS in .env. Blue Iris locks accounts after repeated "
        "failed logins."
    ),
    ErrorCode.CAMERA_NOT_FOUND: (
        "Call bi_list_cameras to confirm the short name. Camera short names are case-sensitive."
    ),
    ErrorCode.ALERT_NOT_FOUND: "Call bi_list_alerts and use a 'path' from the returned array.",
    ErrorCode.CLIP_NOT_FOUND: "Call bi_list_alerts and use a 'clip' path from the returned array.",
    ErrorCode.VALIDATION_FAILED: (
        "Check the tool's documented parameters in AGENTS.md § Tool inventory."
    ),
    ErrorCode.MUTATIONS_DISABLED: (
        "Set BI_MCP_ALLOW_MUTATIONS=1 in bi-mcp/.env to enable mutating tools. "
        "Read AGENTS.md § Mutation patterns first."
    ),
    ErrorCode.PTZ_FAILED: (
        "Confirm the preset number exists via bi_get_ptz_status, and that the camera "
        "has PTZ enabled in Blue Iris."
    ),
    ErrorCode.TRIGGER_FAILED: (
        "Confirm the camera short name with bi_list_cameras. BI may reject the trigger "
        "if the camera is disabled or offline."
    ),
    ErrorCode.PROFILE_SET_FAILED: (
        "Confirm the profile name/index against bi_get_session profiles[]. Profile names "
        "are case-sensitive."
    ),
    ErrorCode.STALE_REG: (
        "Re-export the camera: right-click in Blue Iris → Camera settings → Copy/import → "
        "Export, save into bi-mcp/cam settings/."
    ),
    ErrorCode.REG_PARSE_FAILED: (
        "The .reg file may be corrupt or in the wrong format. Re-export it from Blue Iris."
    ),
    ErrorCode.BI_ERROR: "Check Blue Iris's Status → Messages window for details.",
}


def create_error_response(
    code: ErrorCode,
    message: str,
    suggestion: str | None = None,
    **context: Any,
) -> dict[str, Any]:
    """Build a structured ``{success:False, error:{...}, ...}`` payload.

    ``suggestion`` defaults to the entry in ``DEFAULT_SUGGESTIONS`` for the code.
    Extra ``context`` kwargs are merged into the top level.
    """
    return {
        "success": False,
        "error": {
            "code": code.value,
            "message": message,
            "suggestion": suggestion or DEFAULT_SUGGESTIONS.get(code, ""),
        },
        **context,
    }


_KIND_TO_CODE: dict[str, ErrorCode] = {
    "unreachable": ErrorCode.BI_UNREACHABLE,
    "auth": ErrorCode.AUTH_FAILED,
    "admin_auth": ErrorCode.ADMIN_AUTH_FAILED,
    "admin_required": ErrorCode.ADMIN_REQUIRED,
    "not_found": ErrorCode.CAMERA_NOT_FOUND,
    "bad_request": ErrorCode.VALIDATION_FAILED,
    "mutations_disabled": ErrorCode.MUTATIONS_DISABLED,
    "stale_reg": ErrorCode.STALE_REG,
    "bi_error": ErrorCode.BI_ERROR,
}


def code_from_exception(exc: BiError) -> ErrorCode:
    """Map a raised BiError to its ErrorCode counterpart."""
    return _KIND_TO_CODE.get(exc.kind, ErrorCode.BI_ERROR)
