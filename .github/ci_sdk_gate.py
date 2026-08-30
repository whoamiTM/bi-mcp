"""CI-only pytest plugin: prove the SDK-generation-gated tests actually ran.

`tests/unit/test_server_sdk_compat.py` gates 13 tests behind
`@_needs_1x_decorator`, a `skipif` that fires when the installed MCP SDK has
no low-level decorator API (i.e. on 2.x). Those tests are the only ones that
drive the REAL 1.x decorator, so on the 1.x CI leg they are the entire reason
the leg exists. If they silently skipped there, the leg would pass vacuously
and look like coverage while providing none.

This plugin finds them by their skipif REASON (not by node id or count, so it
survives renames and reordering) and fails the run when the observed number
that EXECUTED does not match what the leg declared via --require-1x-gated=N.

Counting is done from the report stream rather than by parsing pytest's
short summary, because pytest groups identical skip reasons onto one line —
the 13 tests appear as only 10 summary lines, so line-counting undercounts.
"""

from __future__ import annotations

import pytest

_REASON = "needs a 1.x SDK"
_MARK = "gated_1x"

_executed: list[str] = []
_skipped: list[str] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-1x-gated",
        type=int,
        default=None,
        metavar="N",
        help="Fail unless exactly N 1.x-decorator-gated tests actually executed.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for mark in item.iter_markers("skipif"):
            if _REASON in str(mark.kwargs.get("reason", "")):
                item.user_properties.append((_MARK, True))
                break


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # A skipif fires at setup; a test that runs reports its outcome at call.
    if not (report.when == "call" or (report.when == "setup" and report.skipped)):
        return
    if not any(key == _MARK for key, _ in report.user_properties):
        return
    (_skipped if report.skipped else _executed).append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    want = session.config.getoption("--require-1x-gated")
    ran, skipped = len(_executed), len(_skipped)
    print(f"\n[sdk-gate] 1x-decorator-gated tests: executed={ran} skipped={skipped}")
    if want is None:
        return
    if ran != want:
        print(
            f"[sdk-gate] FAIL: expected {want} gated test(s) to EXECUTE, but "
            f"{ran} ran and {skipped} skipped. This leg is not testing the "
            f"1.x branch it exists to cover — treat it as no coverage, not a pass."
        )
        session.exitstatus = 1
