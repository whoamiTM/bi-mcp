"""Regression: `bi_update_record`'s not-a-clip remap must not be steerable
by the caller's own `path`.

`_read_record_state` remaps a BARE `BiError` from `clipstats` to `BiBadRequest`
("this record isn't clip-backed") when BI's reason contains one of
`_CLIPSTATS_NOT_A_CLIP_FRAGMENTS`. That match used to run against the whole
wrapped message — which carries two pieces of caller-controlled text:

  * the wrapper frame itself, and
  * `path`, which is free text AND which BI echoes back inside its own reason
    ("Not found: @no clip").

So `path="@no clip"` made BI's plain "not found" self-diagnose as "not a
clip-backed record": still a loud failure, but the wrong diagnosis pointed at
the wrong fix. This is the same false-positive class as the export-graduation
defect (see `test_export_graduation_casing.py`); the two matchers drifted
because each parsed the message its own way.

Substring matching against BI's remaining text is INTENTIONAL and must stay —
BI's real not-a-clip wording is undocumented and varies. Only the search space
is narrowed.
"""

from __future__ import annotations

import pytest

from bi_mcp.client import BiClient, BiClients
from bi_mcp.errors import BiBadRequest, BiError


@pytest.fixture(autouse=True)
def _allow_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "1")


def _read_state():
    from bi_mcp.tools.tools_mutations import _read_record_state

    return _read_record_state


def _clients(reason: str) -> BiClients:
    """Every POST fails with `reason`, so the real `_call_with_auth_retry`
    builds the real wrapped `BiError` the remap then has to interpret."""
    c = BiClient("host", 81, "u", "p")
    c.session = "sess"
    c.login_data = {"admin": True}
    c.login = lambda: None  # type: ignore[method-assign]
    c._post = lambda body: {"result": "fail", "data": {"reason": reason}}  # type: ignore[method-assign]
    return BiClients(read=c, admin=None)


# Paths that plant a fragment. BI echoes each one back inside a genuine
# "Not found", so the ONLY reason the phrase appears is the caller put it there.
PLANTED_PATHS = [
    "@no clip",
    "@not bvr",
    "@not a clip",
    "@my clip not bvr backup",   # fragment embedded mid-path
    "@NO CLIP",                  # casing must not launder it either
    "@x failed: no clip",        # + a wrapper separator the caller controls
]


@pytest.mark.parametrize("path", PLANTED_PATHS)
def test_planted_path_does_not_manufacture_a_not_a_clip_verdict(path: str) -> None:
    """BI said "not found". The caller's own path must not upgrade that into a
    confident (and wrong) "your record isn't clip-backed"."""
    with pytest.raises(BiError) as ei:
        _read_state()(_clients(f"Not found: {path}"), path)
    assert type(ei.value) is BiError, (
        f"path={path!r} steered the diagnosis: BI's reason was a plain "
        f"'Not found' and the remap must not fire"
    )


def test_plain_not_found_still_raises_bare_bierror() -> None:
    """Control: the ordinary case the planted paths are compared against."""
    with pytest.raises(BiError) as ei:
        _read_state()(_clients("Not found: @1"), "@1")
    assert type(ei.value) is BiError


# BI's own not-a-clip phrasings. Each must STILL remap — narrowing the search
# space must not cost the feature. These carry no caller text at all.
GENUINE_REASONS = [
    "Clip not BVR",
    "clip not bvr",
    "CLIP NOT BVR",
    "record is not a clip",
    "no clip file for this record",
    "Not BVR",
]


@pytest.mark.parametrize("reason", GENUINE_REASONS)
def test_genuine_bi_not_a_clip_reason_still_remaps(reason: str) -> None:
    """Substring semantics against BI's authored text are preserved."""
    with pytest.raises(BiBadRequest):
        _read_state()(_clients(reason), "@1")


def test_genuine_reason_still_remaps_even_when_path_is_echoed() -> None:
    """The elision removes the path, not the whole reason: BI genuinely
    reporting not-a-clip AND echoing the path must still remap."""
    with pytest.raises(BiBadRequest):
        _read_state()(_clients("@1: clip not bvr"), "@1")


def test_typed_subclass_still_propagates_untouched() -> None:
    """The `type(e) is BiError` guard is bare-only; a typed subclass must
    escape the remap entirely."""
    from bi_mcp.errors import BiUnreachable

    c = BiClient("host", 81, "u", "p")
    c.session = "sess"
    c.login_data = {"admin": True}
    c.login = lambda: None  # type: ignore[method-assign]

    def _boom(body):  # noqa: ANN001, ARG001
        raise BiUnreachable("Blue Iris cmd=clipstats failed: clip not bvr")

    c._post = _boom  # type: ignore[method-assign]
    with pytest.raises(BiUnreachable):
        _read_state()(BiClients(read=c, admin=None), "@1")


def test_remap_actually_calls_the_shared_extraction_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the anti-duplication property the brief required: the remap must
    go THROUGH the shared anchored parse, not a private re-implementation.

    Asserting the import alone would be vacuous — the name can be imported and
    never called. The wrapper at this call site is invariant (`cmd` is the
    literal "clipstats") so no black-box input can distinguish "extract then
    elide" from "elide only"; a spy is the only instrument that can. Without
    it, a future call site with a caller-influenced `cmd` re-opens the defect
    silently.
    """
    import sys

    import bi_mcp.client as client_mod

    # Resolve the function FIRST, then patch the module object it actually
    # came from. `test_mutation_gate.py` drops `bi_mcp.tools.*` from
    # sys.modules and reloads, so a plain `import bi_mcp.tools.tools_mutations`
    # here can hand back a DIFFERENT module object than the one holding the
    # function under test — patching that one would spy on nothing and the
    # assertion below would fail for the wrong reason.
    fn = _read_state()
    tm = sys.modules[fn.__module__]

    calls: list[str] = []

    def _spy(message: str) -> str:
        calls.append(message)
        return client_mod.bi_authored_reason(message)

    monkeypatch.setattr(tm, "bi_authored_reason", _spy)

    with pytest.raises(BiError):
        fn(_clients("Not found: @1"), "@1")

    assert calls == ["Blue Iris cmd=clipstats failed: Not found: @1"], (
        "the remap must delegate to the shared extraction helper; a private "
        "copy of the parsing is how these matchers drifted apart before"
    )


def test_success_path_returns_memo_and_flags() -> None:
    """Positive control: the remap logic sits on the failure path only."""
    c = BiClient("host", 81, "u", "p")
    c.session = "sess"
    c.login_data = {"admin": True}
    c.login = lambda: None  # type: ignore[method-assign]
    c._post = lambda body: {  # type: ignore[method-assign]
        "result": "success",
        "data": {"memo": "hello", "flags": 3},
    }
    assert _read_state()(BiClients(read=c, admin=None), "@1") == ("hello", 3)
