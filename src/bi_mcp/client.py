"""Blue Iris HTTP/JSON client.

Implements the two-step MD5 session handshake documented in
``BlueIris_Manual.md`` § *JSON Interface* (line 8353+):

  1. POST {"cmd":"login"} → server returns ``result:"fail"`` + a session token
  2. POST {"cmd":"login", "session":..., "response": MD5("user:session:pass")}
     → server returns ``result:"success"`` + login data

The session token is cached and reused for all subsequent calls. If a call
returns ``result:"fail"`` mid-session, the client logs in once more and
retries the call transparently. Auth failures (wrong user/pass) are NOT
retried — Blue Iris has built-in brute-force lockout.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Any, Iterator

import httpx

from .errors import (
    BiAdminAuthFailed,
    BiAuthFailed,
    BiBadRequest,
    BiError,
    BiNotFound,
    BiUnreachable,
    BiVerifyAuthBlip,
    BiVerifyUnreachable,
)
from .logging_setup import get_logger

log = get_logger()

DEFAULT_TIMEOUT = 10.0


class BiClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not host:
            raise BiBadRequest("BI_HOST is empty in .env")
        if not user:
            raise BiBadRequest("BI_USER is empty in .env")
        if not password:
            raise BiBadRequest("BI_PASS is empty in .env")

        self.host = host
        self.port = int(port)
        self.user = user
        self._password = password
        self.session: str | None = None
        self.login_data: dict[str, Any] | None = None

        self._http = httpx.Client(
            base_url=f"http://{self.host}:{self.port}",
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    # ----- public API --------------------------------------------------

    def login(self) -> dict[str, Any]:
        """Perform the two-step MD5 handshake. Returns the login response ``data``."""
        log.debug("Login step 1: requesting session for user=%s", self.user)
        step1 = self._post({"cmd": "login"})

        # Step 1 always returns result:"fail" + session. If it returns success
        # immediately, the server is in no-LAN-password mode and we're done.
        if step1.get("result") == "success":
            self.session = step1.get("session")
            self.login_data = step1.get("data", {})
            log.debug("Login: server accepted unauthenticated session")
            return self.login_data

        sess = step1.get("session")
        if not sess:
            raise BiError("Blue Iris login step 1 did not return a session token")

        token = hashlib.md5(f"{self.user}:{sess}:{self._password}".encode()).hexdigest()
        log.debug("Login step 2: sending MD5 response")
        step2 = self._post({"cmd": "login", "session": sess, "response": token})

        if step2.get("result") != "success":
            reason = (step2.get("data") or {}).get("reason", "rejected")
            raise BiAuthFailed(f"Blue Iris rejected login: {reason}")

        self.session = sess
        self.login_data = step2.get("data", {})
        log.info("Login successful; BI version=%s", self.login_data.get("version"))
        return self.login_data

    def call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Call a BI cmd and return the full response envelope (not just `data`).

        Most callers want ``call()`` which unwraps ``data``. ``call_raw()`` is
        for cmds like ``trigger``/``ptz`` (write side) that return
        ``{result:"success"}`` with no ``data``, where the caller wants to
        verify ``result`` rather than treat absence-of-data as the response.
        """
        if not self.session:
            self.login()
        body: dict[str, Any] = {"cmd": cmd, "session": self.session, **payload}
        log.debug("Call (raw) cmd=%s", cmd)
        resp = self._post(body)
        if resp.get("result") == "fail":
            log.info("cmd=%s returned fail; attempting one session re-login + retry", cmd)
            self.session = None
            self.login()
            body["session"] = self.session
            resp = self._post(body)
            if resp.get("result") == "fail":
                reason = (resp.get("data") or {}).get("reason") or resp.get("data") or "no reason given"
                raise BiError(f"Blue Iris cmd={cmd} failed: {reason}")
        return resp

    def call(self, cmd: str, **payload: Any) -> Any:
        """Call a Blue Iris JSON cmd. Logs in lazily, retries once on session expiry."""
        if not self.session:
            self.login()

        body: dict[str, Any] = {"cmd": cmd, "session": self.session, **payload}
        log.debug("Call cmd=%s", cmd)
        resp = self._post(body)

        if resp.get("result") == "fail":
            # Could be expired session OR legitimate cmd failure. Distinguish
            # by retrying login + call once; if it still fails, surface it.
            log.info("cmd=%s returned fail; attempting one session re-login + retry", cmd)
            self.session = None
            self.login()
            body["session"] = self.session
            resp = self._post(body)
            if resp.get("result") == "fail":
                reason = (resp.get("data") or {}).get("reason") or resp.get("data") or "no reason given"
                raise BiError(f"Blue Iris cmd={cmd} failed: {reason}")

        # Most cmds return result:"success" + data. A few return data inline.
        if "data" in resp:
            return resp["data"]
        return resp

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        """GET a non-/json endpoint (e.g. `/image/<short>`) with the session
        token attached as a query param. Returns (body_bytes, content_type).

        Re-logs in and retries once on 401/403 in case the session expired,
        matching the auth-retry behavior of ``call()``.
        """
        if not self.session:
            self.login()
        body, ctype, status = self._get_raw(path, {**params, "session": self.session})
        if status in (401, 403):
            log.info("GET %s returned %d; re-login + retry", path, status)
            self.session = None
            self.login()
            body, ctype, status = self._get_raw(path, {**params, "session": self.session})
        if status == 404:
            raise BiNotFound(f"Blue Iris HTTP 404 on {path}")
        if status >= 400:
            raise BiBadRequest(f"Blue Iris returned HTTP {status} on {path}: {body[:200]!r}")
        return body, ctype

    def _get_raw(self, path: str, params: dict[str, Any]) -> tuple[bytes, str, int]:
        try:
            r = self._http.get(path, params=params)
        except httpx.ConnectError as e:
            raise BiUnreachable(f"Cannot connect to Blue Iris at {self.host}:{self.port}: {e}") from e
        except httpx.TimeoutException as e:
            raise BiUnreachable(f"Blue Iris at {self.host}:{self.port} timed out: {e}") from e
        except httpx.HTTPError as e:
            raise BiUnreachable(f"HTTP error talking to Blue Iris: {e}") from e
        return r.content, r.headers.get("content-type", ""), r.status_code

    def close(self) -> None:
        self._http.close()

    # ----- internals ---------------------------------------------------

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._http.post("/json", json=body)
        except httpx.ConnectError as e:
            raise BiUnreachable(f"Cannot connect to Blue Iris at {self.host}:{self.port}: {e}") from e
        except httpx.TimeoutException as e:
            raise BiUnreachable(f"Blue Iris at {self.host}:{self.port} timed out: {e}") from e
        except httpx.HTTPError as e:
            raise BiUnreachable(f"HTTP error talking to Blue Iris: {e}") from e

        if r.status_code >= 500:
            raise BiError(f"Blue Iris returned HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code == 404:
            raise BiNotFound(f"Blue Iris HTTP 404 on /json — is the web server enabled?")
        if r.status_code >= 400:
            raise BiBadRequest(f"Blue Iris returned HTTP {r.status_code}: {r.text[:200]}")

        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise BiError(f"Blue Iris returned non-JSON: {r.text[:200]}") from e

    def __enter__(self) -> "BiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class BiClients:
    """Pair of Blue Iris clients: the default read-only user, plus an optional
    admin user for cmds that BI gates behind admin rights (e.g. `camconfig`,
    `log`).

    Three configuration shapes are supported:

      1. ``BI_ADMIN_USER`` + ``BI_ADMIN_PASS`` set → ``_explicit_admin`` is
         a separate ``BiClient`` instance; ``admin`` returns it always.
      2. ``BI_USER`` is itself an admin and no admin env vars are set → the
         ``read`` client doubles as the admin client. We discover this
         lazily: the first time ``admin`` is queried we check
         ``read.login_data["admin"]``, logging in if needed.
      3. ``BI_USER`` is non-admin and no admin env vars are set → ``admin``
         resolves to ``None`` (after the lazy probe).

    Shape (2) used to be resolved eagerly in ``from_env()`` via an extra
    startup ``read.login()``. That stalled MCP initialization on slow/dead
    BI hosts and permanently cached ``admin=None`` after a single transient
    failure. The lazy approach avoids both problems: BI is only contacted
    when something actually needs auth, and a transient failure leaves the
    probe re-armed for the next call.
    """

    def __init__(self, read: BiClient, admin: BiClient | None):
        self.read = read
        # The explicit admin client (configuration shape 1). Stays None for
        # shapes 2 and 3.
        self._explicit_admin = admin

    @property
    def admin(self) -> BiClient | None:
        """The admin client, or None if no admin path is available.

        Side-effect-free lookup. If no explicit admin client was configured,
        falls back to ``read`` *only if* ``read`` has already logged in and
        the login response reports admin=true. If ``read`` hasn't logged in
        yet, returns None — callers that ACTUALLY need admin should call
        ``resolve_admin()`` instead, which will trigger a login as needed.

        This split exists so that capability checks like
        ``if client.admin is not None: try admin path else fall back`` stay
        cheap and don't initiate network I/O.
        """
        if self._explicit_admin is not None:
            return self._explicit_admin
        if self.read.login_data is not None and self.read.login_data.get("admin"):
            return self.read
        return None

    def resolve_admin(self) -> BiClient | None:
        """Like ``admin`` but actively logs in to find out.

        When no explicit admin client is configured, this forces a
        ``read.login()`` (if it hasn't happened yet) so the lazy BI_USER-as-
        admin probe can answer correctly even on a fresh process whose first
        tool call is admin-gated. Returns None only after BI has confirmed
        the user lacks admin (or if the login itself fails — caller's
        exception handler should surface that).

        Capability-check call sites should keep using ``.admin`` (cheap,
        no I/O). Admin-required call sites use this.
        """
        if self._explicit_admin is not None:
            return self._explicit_admin
        if self.read.login_data is None:
            self.read.login()
        if self.read.login_data is not None and self.read.login_data.get("admin"):
            return self.read
        return None

    def admin_or_raise(self) -> BiClient:
        admin = self.resolve_admin()
        if admin is None:
            raise BiAuthFailed(
                "This tool requires admin BI credentials. Set BI_ADMIN_USER "
                "and BI_ADMIN_PASS in bi-mcp/.env (create a dedicated admin "
                "user in Blue Iris → Settings → Users), or grant admin to the "
                "existing BI_USER."
            )
        return admin

    @contextlib.contextmanager
    def fresh_admin_session(self) -> Iterator[BiClient]:
        """Yield a throwaway admin ``BiClient`` for verify-after-write reads.

        Brand-new httpx session and BI session token, cloned from the admin
        user's credentials — never touches the shared admin singleton.
        Automatically closed on exit.

        Why this exists: BI 5.9.9.71 returns stale camconfig/camlist reads
        to the session that issued the write for ~2-3s. Defeating that
        staleness used to mean clearing the shared singleton's session token
        mid-call, which corrupted overlapping tool calls (Codex review
        2026-05-22). A throwaway client per verify call defeats staleness
        without the singleton hazard.

        Pair with :meth:`verify_call` (preferred) so auth blips during
        verification are surfaced as ``BiVerifyInconclusive`` rather than
        hard admin-auth errors.
        """
        src = self.admin_or_raise()
        fresh = BiClient(host=src.host, port=src.port, user=src.user, password=src._password)
        try:
            yield fresh
        finally:
            fresh.close()

    @staticmethod
    def verify_call(fresh: BiClient, cmd: str, **payload: Any) -> Any:
        """Run a post-write verification read through a fresh admin client.

        Converts blip-class verify-side failures into typed subclasses of
        ``BiVerifyInconclusive``:

          * ``BiAuthFailed`` → ``BiVerifyAuthBlip`` (kind=``verify_auth_blip``).
            Throwaway login could not authenticate. Causes range from
            transient (BI session pressure, parallel admin logins) to
            durable (creds rotated, account locked). Callers seeing this
            repeatedly should investigate creds rather than blind-retry.
          * ``BiUnreachable`` → ``BiVerifyUnreachable``
            (kind=``verify_unreachable``). Network blip / timeout / BI
            restart. Almost always transient.

        Rationale for distinct kinds: a verify-side auth failure deserves
        different handling than a network blip — a single boolean
        "inconclusive" flag would hide durable creds breakage behind a
        transient-looking flag. Surfacing the kind lets callers escalate
        auth-class blips on repeat without us having to do (brittle)
        stateful classification at the verify layer.

        Rationale for raising at all (rather than returning): the write
        already succeeded (verify only runs after a success reply), so
        these are not "BI is down" / "creds are wrong" failures from the
        caller's perspective — they are "write landed, post-read couldn't
        confirm" situations. Dispatchers catch and convert to
        ``verified=False`` + structured ``verify_error_kind`` in the
        response, while keeping ``ok=True`` (the write *was* accepted).

        Structural / logic errors (``BiBadRequest``, ``BiNotFound``, and
        bare ``BiError`` for malformed responses) propagate unchanged —
        those indicate real bugs in the verify path that the caller needs
        to see loudly.
        """
        try:
            return fresh.call(cmd, **payload)
        except BiAuthFailed as e:
            raise BiVerifyAuthBlip(
                f"verify read cmd={cmd} could not authenticate: {e}"
            ) from e
        except BiUnreachable as e:
            raise BiVerifyUnreachable(
                f"verify read cmd={cmd} could not reach Blue Iris: {e}"
            ) from e

    def admin_call(self, cmd: str, **payload: Any) -> Any:
        """Call an admin-gated BI cmd. Re-tags BiAuthFailed from the admin
        client as BiAdminAuthFailed so the hint points at the right env vars.

        Triggers a ``read.login()`` if no explicit admin client is configured
        and BI hasn't been contacted yet — see ``resolve_admin``.
        """
        admin = self.resolve_admin()
        assert admin is not None  # callers should pre-check via .admin
        try:
            return admin.call(cmd, **payload)
        except BiAuthFailed as e:
            raise BiAdminAuthFailed(str(e)) from e

    def admin_call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Like ``call_raw`` but routed through the admin client."""
        admin = self.resolve_admin()
        assert admin is not None
        try:
            return admin.call_raw(cmd, **payload)
        except BiAuthFailed as e:
            raise BiAdminAuthFailed(str(e)) from e

    @property
    def bi_version(self) -> str | None:
        """Connected BI version (best-effort; populated after first login)."""
        if self.read.login_data:
            return self.read.login_data.get("version")
        return None

    def admin_login(self) -> dict[str, Any]:
        """Force admin auth.

        For shape (1) — explicit admin client — logs in if needed. For
        shape (2) — read-doubles-as-admin — forces ``read.login()`` and
        returns its data only if the user actually has admin. Raises
        ``BiAdminAuthFailed`` on auth failure, ``BiAuthFailed`` on
        no-admin-available (consistent with ``admin_or_raise``).
        """
        if self._explicit_admin is not None:
            if self._explicit_admin.login_data is not None:
                return self._explicit_admin.login_data
            try:
                return self._explicit_admin.login()
            except BiAuthFailed as e:
                raise BiAdminAuthFailed(str(e)) from e
        # No explicit admin — probe `read`.
        if self.read.login_data is None:
            self.read.login()
        if not (self.read.login_data or {}).get("admin"):
            raise BiAuthFailed(
                "This tool requires admin BI credentials. The configured "
                "BI_USER does not have admin enabled. Either grant admin to "
                "that user in Blue Iris → Settings → Users, or set "
                "BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env to a separate "
                "admin user."
            )
        return self.read.login_data  # type: ignore[return-value]

    def call(self, cmd: str, **payload: Any) -> Any:
        """Delegate to the read-only client. Tools that need admin should
        call `.admin_call(...)` explicitly."""
        return self.read.call(cmd, **payload)

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        """Delegate to the read-only client's GET helper."""
        return self.read.get_bytes(path, **params)

    def call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Delegate to the read-only client's raw cmd path.

        Used by the mutation tools (``bi_trigger_camera``, ``bi_set_ptz_preset``,
        ``bi_set_profile``) which need the full response envelope
        (``{result:"success"}``) rather than the unwrapped ``data`` block —
        BI's write cmds return success markers, not data.
        """
        return self.read.call_raw(cmd, **payload)

    @property
    def login_data(self) -> dict[str, Any] | None:
        return self.read.login_data

    def login(self) -> dict[str, Any]:
        return self.read.login()

    def close(self) -> None:
        self.read.close()
        if self._explicit_admin is not None:
            self._explicit_admin.close()


def from_env() -> BiClients:
    """Build a BiClients pair from environment variables.

    Required:  BI_HOST, BI_PORT, BI_USER, BI_PASS  (read-only user)
    Optional:  BI_ADMIN_USER, BI_ADMIN_PASS        (admin user for camconfig/log)

    Does NOT contact Blue Iris. If no explicit admin creds are given,
    ``BiClients.admin`` lazily checks whether ``BI_USER`` itself has admin
    rights on first use — see the class docstring.
    """
    import os

    host = os.environ.get("BI_HOST", "")
    port = int(os.environ.get("BI_PORT", "81") or "81")

    read = BiClient(
        host=host,
        port=port,
        user=os.environ.get("BI_USER", ""),
        password=os.environ.get("BI_PASS", ""),
    )

    admin_user = os.environ.get("BI_ADMIN_USER", "").strip()
    admin_pass = os.environ.get("BI_ADMIN_PASS", "")
    # Both or neither — a half-filled admin config is almost always a typo and
    # would silently downgrade admin-gated tools to the shallow fallback.
    if bool(admin_user) != bool(admin_pass):
        which_set = "BI_ADMIN_USER" if admin_user else "BI_ADMIN_PASS"
        which_missing = "BI_ADMIN_PASS" if admin_user else "BI_ADMIN_USER"
        raise BiBadRequest(
            f"{which_set} is set in .env but {which_missing} is empty. "
            "Set both to enable admin-gated tools, or unset both to run read-only."
        )

    explicit_admin: BiClient | None = None
    if admin_user and admin_pass:
        explicit_admin = BiClient(host=host, port=port, user=admin_user, password=admin_pass)
    # Else: no explicit admin. BI is NOT contacted here — the `BI_USER as
    # admin` fallback is resolved lazily by `BiClients.admin` on first use.

    return BiClients(read=read, admin=explicit_admin)
