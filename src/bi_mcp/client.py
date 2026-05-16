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

import hashlib
import json
from typing import Any

import httpx

from .errors import BiAuthFailed, BiBadRequest, BiError, BiNotFound, BiUnreachable
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


def from_env() -> BiClient:
    """Build a BiClient from environment variables (BI_HOST, BI_PORT, BI_USER, BI_PASS)."""
    import os

    return BiClient(
        host=os.environ.get("BI_HOST", ""),
        port=int(os.environ.get("BI_PORT", "81") or "81"),
        user=os.environ.get("BI_USER", ""),
        password=os.environ.get("BI_PASS", ""),
    )
