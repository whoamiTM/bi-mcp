#!/usr/bin/env bash
# Advisory test runner — fires on PostToolUse for Edit/Write under bi-mcp.
#
# Behavior:
#   • Silent on pass (no output → no extra tokens, no noise).
#   • Loud on fail (clear header + pytest short summary).
#   • Exit 0 always — this is advisory, not blocking. Claude sees the
#     output and decides whether to fix immediately or defer.
#
# Hook payload (PostToolUse) arrives on stdin as JSON. We only care about
# the edited file path; if it isn't under src/ or tests/, we skip silently.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTEST="$REPO_ROOT/.venv/bin/pytest"

# Read hook payload from stdin (best-effort — tolerate missing/odd shape).
payload="$(cat || true)"

# Extract tool_input.file_path with a tiny inline python (jq isn't a hard
# dep on every host). Falls through to empty string on any parse failure.
file_path="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
    print((d.get("tool_input") or {}).get("file_path", ""))
except Exception:
    print("")
' 2>/dev/null)"

# Only run when an edit landed under src/ or tests/. Anything else
# (README tweaks, AGENTS.md, settings) doesn't affect test behaviour.
case "$file_path" in
  "$REPO_ROOT"/src/bi_mcp/*|"$REPO_ROOT"/tests/*) ;;
  *) exit 0 ;;
esac

# Pytest must exist — if the venv is gone, surface that once and bail.
if [[ ! -x "$PYTEST" ]]; then
  echo "⚠️  bi-mcp tests skipped: $PYTEST not found. Run: .venv/bin/pip install 'pytest>=8'" >&2
  exit 0
fi

# Run quietly; capture output so we can decide whether to emit anything.
output="$("$PYTEST" "$REPO_ROOT/tests/unit/" -q --no-header --tb=line 2>&1)"
rc=$?

if [[ $rc -ne 0 ]]; then
  # Loud header so the failure stands out in the conversation transcript.
  echo "❌ bi-mcp tests FAILED after edit to ${file_path#$REPO_ROOT/}" >&2
  echo "$output" >&2
fi

# Always exit 0 — advisory, never blocks the edit.
exit 0
