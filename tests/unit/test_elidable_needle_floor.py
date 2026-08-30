"""`_MIN_ELIDABLE_NEEDLE` must stay <= the shortest fragment any ELIDING site
matches — mechanically, not by prose.

The constant's whole justification is "a needle shorter than this cannot
CONTAIN a matcher fragment, so skipping the elision cannot forge a match".
That is only true while every fragment is at least that long. The claim was
previously asserted in a comment and by nothing else, so adding a 6-char
fragment (`"no bvr"`) anywhere would silently disable the guard for it: a
caller planting exactly that 6-char string as `path`/`short` would slip past
elision and forge the match.

Discovery is by AST walk over the whole package, NOT a hardcoded list of
lists — a list of lists rots exactly like the prose did. Every call to
`_elide_caller_text` is found, then the fragment container consulted against
its result is resolved from the enclosing module. A NEW eliding site added
later is therefore covered automatically, and a site whose fragments this
test cannot resolve FAILS rather than passing vacuously.
"""

from __future__ import annotations

import ast
import importlib
import io
import pathlib
import tokenize

import pytest

from bi_mcp.client import _MIN_ELIDABLE_NEEDLE

SRC = pathlib.Path(importlib.import_module("bi_mcp").__file__).parent
ELIDER = "_elide_caller_text"

# Scope kinds whose body this test walks looking for the elider's result and
# the matchers that consult it. Anything that can lexically contain a call
# belongs here, not just `def`: a module body, a class body, a lambda, and
# every comprehension form (each of which is its own scope at runtime, and
# each of which `ast.walk` reaches as a distinct node). Nested `def`s and
# methods need no entry — they ARE `FunctionDef`s, and `ast.walk` visits them
# on their own as well as via their parent.
#
# This tuple is a coverage OPTIMISATION, never the safety property: see the
# `claimed`/`unclaimed` backstop in `_discover_eliding_sites`, which fails the
# suite on any call no listed scope claims. Adding to this tuple turns a loud
# failure into a real audit; forgetting to add to it cannot turn a real audit
# into silence.
_AUDITABLE_SCOPES = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(["bi_mcp", *parts])


def _iter_source_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _names_in(node: ast.AST) -> set[str]:
    """Every bare Name loaded anywhere under ``node``."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _literal_elements(node: ast.AST, module) -> list[str] | None:
    """Every element of an inline container, or ``None`` if any is unreadable.

    NOT REACHED BY ANY REAL SITE as of this writing: instrumenting
    `_discover_eliding_sites` over the whole package counts 2 calls into
    `_resolve_fragments` (the two `Name`-resolved sites) and 0 into this
    function — all three eliding sites either name a module-level container or
    spell their needles as inline `Constant`s handled by
    `_fragments_for_matcher`. It guards a hypothetical future site that writes
    its container inline, and is covered only by this file's synthetic cases.
    Treat it as unexercised-in-practice: do not cite it as evidence a shape is
    handled in production, and re-measure before relying on it.

    Element-WISE on purpose. This used to be a `ast.walk` for string
    `Constant`s anywhere under the container, which is a fail-OPEN shape: a
    walk cannot tell an element from a fragment of one, so any element it did
    not understand simply contributed nothing and the container still resolved
    truthy. `(*_EXTRA, "not a clip")` resolved to `["not a clip"]` alone — the
    starred half, which can hold a fragment of any length, vanished silently
    and its needles never reached the floor. The walk was also wrong in the
    other direction, harvesting sub-expression strings that are not elements at
    all (`("a" + "b",)` yielded BOTH `"a"` and `"b"`, neither of them the
    needle `"ab"` actually iterated).

    So each element must resolve on its own terms, and anything else makes the
    whole container ``None``:

    * a string `Constant` IS the fragment (implicit concatenation, `"a" "b"`,
      is already folded into one `Constant` by the parser, so it needs no
      special case);
    * `*C` is resolved recursively — a starred module-level container is
      readable, so rejecting it outright would fail an honest refactor;
    * a non-`str` `Constant` (`b"x"`, `7`, `None`) is NOT a fragment. It cannot
      be a needle for an `in` against text, so a container holding one is not
      a fragment container this test understands;
    * an EMPTY `str` (`""`) is not a measurable fragment either — a 0-char
      needle is `in` every message, so it defeats the matcher rather than
      merely evading the floor;
    * a `Name`, an f-string, a conditional, a nested container, a call, a
      BinOp: the value is a runtime property, so its length is unknowable.
    """
    out: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Starred):
            # `*C` contributes C's elements; readable only if C is.
            inner = _resolve_fragments(elt.value, module)
            if inner is None:
                return None
            out.extend(inner)
        elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            # An empty literal (`""`) is a 0-char needle that matches every
            # message; same fail-closed rule as `_resolve_fragments`.
            if not elt.value:
                return None
            out.append(elt.value)
        else:
            return None
    return out


# String methods that cannot express a fragment match, so a haystack passed
# to one needs no fragment accounting. Deliberately a WHITELIST of the
# harmless: `startswith`/`endswith`/`find`/`index`/`count`/`split`/`partition`
# and friends all take a needle whose length the floor must cover, and an
# unknown method is treated as one of those. `strip`/`lower` reshape the text
# without consulting a needle at all.
_NEEDLE_FREE_STR_METHODS = frozenset(
    {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}
)

# Per-USE escape hatch for the one shape that is genuinely undecidable from the
# AST: passing the haystack to a call. `log.debug("...%s", reason)` and
# `re.search("no bvr", reason)` are structurally identical — same node type,
# haystack at the same argument index — so no rule over shape can separate the
# harmless from the needle-bearing. Only the author knows which it is, so the
# author states it, on the line, in the diff, where review can see the claim.
#
# Deliberately NOT a file- or function-level switch: it certifies the single
# haystack Load it sits with and nothing else. A second, uncertified use on
# another line still fails. It also cannot be used to wave through the shapes
# below it — those never consult this marker at all — so it can only ever
# excuse a call argument.
#
# The marker is read from real COMMENT TOKENS, never as a substring of the raw
# line, and three abuses of the substring form are why:
#   1. a string LITERAL containing the text self-certified
#      (`log.debug('needle-free: x', reason)`), so no comment was needed at
#      all — which demolished the "it takes a deliberate untrue comment"
#      premise this hatch rests on, and worse, let an innocent literal that
#      merely quoted the marker silently disable the audit for its line;
#   2. a bare `# needle-free:` with no reason certified, though the failure
#      message demands a `<why>`;
#   3. one honest marker certified a SECOND, needle-bearing use joined to the
#      same physical line by a semicolon or a continuation.
# Tokenizing fixes (1); requiring non-empty `<why>` fixes (2); certifying at
# most ONE haystack Load per line fixes (3) — see `_certified_linenos`.
#
# Its remaining limit, stated plainly: a FALSE certification on a matching call
# (`# needle-free:` over `re.search("no bvr", reason)`) still hides that needle
# from the audit. No AST rule can close that — it is a false claim by the
# author, caught by review, not by parsing. What bounds it now is that the
# certified call can no longer hide behind a sibling: per-matcher resolution
# (see `_fragments_for_matcher`) means the site's OTHER matchers no longer vouch
# for it, so a certified call that is the only consumer fails the site outright.
# The alternative — rejecting every call — is what made honest refactors fail,
# which is worse: a guard people route around guards nothing.
_CERTIFIED_NEEDLE_FREE = "needle-free:"


def _certified_linenos(source: str) -> set[int]:
    """Line numbers carrying a well-formed `# needle-free: <why>` COMMENT.

    Read from `tokenize.COMMENT` tokens so a string literal that merely
    contains the marker text cannot certify itself, and require non-empty
    `<why>` text after the colon so the bare marker the failure message
    forbids is actually rejected.
    """
    out: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            _, _, why = tok.string.partition(_CERTIFIED_NEEDLE_FREE)
            if why.strip():
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # An untokenizable module cannot certify anything; the AST parse that
        # precedes every use of this will have raised on its own.
        return set()
    return out


def _audit_haystack_uses(
    func: ast.AST,
    haystacks: set[str],
    modname: str,
    lineno: int,
    certified: set[int],
) -> None:
    """Fail unless EVERY load of a haystack name is a shape we can account for.

    The floor is only meaningful if this test sees every needle any matcher
    compares against the elided text. Fragment collection understands exactly
    one shape (`<fragment> in <haystack>`), so anything else — `.startswith`,
    `re.search`, `==`, membership in a container, a handoff to a helper — has
    to be REJECTED here rather than skipped, or its needle never reaches the
    length check. Whitelisting the shapes we can prove safe (and failing on
    everything else) is what keeps this fail-closed as the code grows; the
    alternative, blacklisting known-dangerous shapes, rots the moment someone
    reaches for a matcher nobody listed.
    """
    parents: dict[ast.AST, ast.AST] = {}
    spent: set[int] = set()
    for node in ast.walk(func):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    # Sorted by source position so "the first Load on a certified line" is
    # deterministic; `ast.walk` is breadth-first and would otherwise let the
    # spend land on whichever of two same-line uses it happened to reach first.
    loads = sorted(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.Name)
            and n.id in haystacks
            and isinstance(n.ctx, ast.Load)
        ),
        key=lambda n: (n.lineno, n.col_offset),
    )
    for node in loads:
        parent = parents.get(node)
        why: str | None = None
        if isinstance(parent, ast.Compare):
            if parent.left is node:
                # `haystack in CONTAINER` / `haystack == "..."`: the needles
                # live on the other side and are not collected below.
                why = "haystack on the LEFT of a comparison"
            elif not any(isinstance(o, ast.In) for o in parent.ops):
                why = f"non-`in` comparison ({type(parent.ops[0]).__name__})"
        elif isinstance(parent, ast.Attribute):
            # `haystack.<method>` — safe only for the reshaping methods.
            if parent.attr not in _NEEDLE_FREE_STR_METHODS:
                why = f"method call `.{parent.attr}(...)` on the haystack"
        elif isinstance(parent, (ast.Assign, ast.IfExp, ast.Return)):
            # Re-binding/passthrough; the fixed-point loop above already
            # follows the value into its new name.
            pass
        elif isinstance(parent, (ast.BoolOp, ast.FormattedValue)):
            # `if reason and any(...)` and `f"reason={reason}"` consume the
            # haystack's VALUE (truthiness / repr) without comparing it to
            # anything, so neither can hide a needle. This is not a judgement
            # call: a matcher nested inside either shape (`f"{reason.
            # startswith('no bvr')}"`) puts the haystack Name under the inner
            # `Attribute`/`Compare`, which this same walk visits as its own
            # node and judges on its own merits. Whitelisting the outer shape
            # therefore cannot smuggle an inner one past the audit.
            pass
        elif isinstance(parent, ast.Call):
            # The undecidable shape. `log.debug("...%s", reason)` (harmless)
            # and `re.search("no bvr", reason)` (a 6-char needle the floor
            # never sees) are the same node with the haystack at the same
            # argument index, so position-based rules are unsound — rejecting
            # every call is the only safe default. The author can certify one
            # specific line instead; see `_CERTIFIED_NEEDLE_FREE`.
            # A line's certification is SPENT by the first haystack Load on
            # it. One honest `# needle-free:` must not vouch for a second use
            # joined to the same physical line by a semicolon or a backslash
            # continuation (`log.debug("%s", reason); re.search("no bvr",
            # reason)  # needle-free: log arg`) — the marker names one use,
            # and the author only inspected one.
            if node.lineno in certified and node.lineno not in spent:
                spent.add(node.lineno)
            else:
                why = (
                    "haystack passed as an argument to a call. If this call "
                    "cannot consult a needle (a log/format argument, say), "
                    f"certify THIS line with a `# {_CERTIFIED_NEEDLE_FREE} "
                    "<why>` comment (the `<why>` text is required, and one "
                    "marker certifies one use — split a line that has two). "
                    "Do NOT certify a call that matches text (`re.search`, a "
                    "helper predicate) — its needle would escape the floor"
                )
        else:
            why = f"unrecognised use under {type(parent).__name__}"
        assert why is None, (
            f"{modname}:{node.lineno}: {why} — this test can only account for "
            f"the needles of `<fragment> in {node.id}` matches, so it cannot "
            f"prove _MIN_ELIDABLE_NEEDLE covers this one. It FAILS rather than "
            "exempting the matcher silently (a needle shorter than the floor "
            "here would be forgeable). Rewrite the matcher as an `in` check, "
            "or teach this test its shape."
        )


def _elider_local_names(tree: ast.AST) -> set[str]:
    """Every bare name in this module that refers to the elider.

    Call discovery used to accept exactly one spelling — an `ast.Name` whose
    `id` is literally `_elide_caller_text` — which is not a property of the
    CALL, only of how the module happened to import it. Two ordinary,
    non-adversarial spellings were therefore INVISIBLE, and an invisible site
    contributes no fragments, so its needles never reached the floor:

        from ..client import _elide_caller_text as _e   ->  _e(...)
        _e = _elide_caller_text                         ->  _e(...)

    Both are the same call to the same function; only the binding differs.
    The set of local names is therefore DERIVED from the module's own
    bindings rather than assumed, which is what stops this rotting on the
    next spelling someone reaches for.
    """
    names = {ELIDER}
    # `from ... import _elide_caller_text as _e` — the alias is the local
    # name; a plain import (no `asname`) rebinds the canonical name.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == ELIDER:
                    names.add(alias.asname or alias.name)
    # `_e = _elide_caller_text` — a rebinding to a name already known to refer
    # to the elider. Iterated to a fixed point so a chain of any length
    # (`_a = _elide_caller_text` then `_b = _a`) is followed, matching how
    # haystack propagation elsewhere in this file handles re-binding.
    for _ in range(len(list(ast.walk(tree)))):
        grown = set(names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id in names:
                    grown |= {
                        t.id for t in node.targets if isinstance(t, ast.Name)
                    }
        if grown == names:
            break
        names = grown
    return names


def _is_elider_call(node: ast.Call, local_names: set[str]) -> bool:
    """Whether ``node`` calls the elider under ANY spelling.

    Two callee shapes reach the same function and both must be discovered:

    * an `ast.Name` whose id is a local binding of the elider — the direct
      spelling plus every alias `_elider_local_names` resolved;
    * an `ast.Attribute` whose `attr` is the elider:
      `client._elide_caller_text(...)` after `from .. import client as _cl`,
      or `bi_mcp.client._elide_caller_text(...)` after `import bi_mcp.client`.
      The attribute NAME is matched rather than the object it is read from,
      deliberately: that object may be spelled any number of ways (`_cl`,
      `client`, `bi_mcp.client`) and resolving it would reintroduce exactly
      the assumption this fix removes. Over-matching some unrelated
      `x._elide_caller_text` costs one spurious audit, which is loud and
      fixable; under-matching is the silent fail-open being closed here.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in local_names
    if isinstance(func, ast.Attribute):
        return func.attr == ELIDER
    return False


def _assert_no_opaque_elider_handoff(
    tree: ast.AST, modname: str, local_names: set[str]
) -> None:
    """FAIL when the elider is REFERENCED without being called outright.

    Some spellings cannot be resolved statically at all: stashing the function
    in a dict or list and calling it through a subscript, wrapping it in
    `functools.partial`, fetching it with `getattr(client, "_elide_caller_text")`,
    or handing it to a decorator. In each, the eventual call node's callee is
    a subscript / partial / computed attribute this walk cannot tie back to
    the elider, so the site would VANISH — the alias fail-open one level
    further out.

    They cannot be resolved, so they are made LOUD instead. This is stricter
    than production needs: every real site calls the function plainly, so the
    strictness costs nothing today and refuses to silently drop a site
    tomorrow. Fail-closed on the undecidable is the same rule the haystack
    audit already applies to call arguments.
    """
    # `getattr(obj, "_elide_caller_text")` — a dynamic lookup whose resulting
    # call this walk cannot follow.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == ELIDER
        ):
            raise AssertionError(
                f"{modname}:{node.lineno}: `{ELIDER}` is fetched dynamically "
                "via `getattr`, so this test cannot find the call it feeds "
                "or the fragments that call gates. Call the function "
                "directly so its needles reach the floor."
            )
    # Every Load of an elider name must BE the callee of its own call. A load
    # that is not is a handoff — into a container, a partial, a decorator, an
    # argument — and the call it eventually reaches is unfindable from here.
    callee_ids = {
        id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    # A Load that FEEDS a rebinding (`_e = _elide_caller_text`) is not an
    # opaque handoff: `_elider_local_names` already resolved that target to
    # the elider, so calls through it ARE discovered. Exempt it, or this
    # check would reject a spelling the walker handles and report it with a
    # message ("stored, wrapped, or passed") that misdescribes the code.
    rebind_ids = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Name)
        and n.value.id in local_names
        and all(isinstance(t, ast.Name) for t in n.targets)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in local_names:
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        if id(node) in callee_ids or id(node) in rebind_ids:
            continue
        raise AssertionError(
            f"{modname}:{node.lineno}: `{node.id}` refers to `{ELIDER}` but "
            "is not called directly here — it is stored, wrapped, or passed "
            "somewhere this test cannot follow (a dict, a `partial`, a "
            "decorator). Its call site's fragments would never reach the "
            "floor, so this FAILS rather than dropping the site silently. "
            "Call the elider directly at the matching site."
        )


def _discover_eliding_sites() -> list[tuple[str, int, list[str]]]:
    """(module, lineno, fragments) for every `_elide_caller_text` call site.

    A site's fragments are whatever string literals the guarding
    comparison/`any(...)` consults — resolved either from module-level
    containers named in that statement, or from inline literals.
    """
    sites: list[tuple[str, int, list[str]]] = []
    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Kept for the per-line certification marker, which is a comment and
        # so invisible to the AST. Read via `tokenize`, not by scanning raw
        # text, so a string literal quoting the marker cannot certify itself.
        certified = _certified_linenos(source)
        modname = _module_name(path)
        # Spellings are resolved from the module's own bindings, never
        # assumed. Matching only a bare `ast.Name` spelled exactly `ELIDER`
        # was the sixth fail-open in this machinery: `_e(...)` after an
        # `import ... as`, and `client._elide_caller_text(...)` after
        # `from .. import client`, are the same call to the same function,
        # but neither entered `calls` — so no haystack audit, no fragment
        # resolution, no floor check, and the site vanished with its needles.
        local_names = _elider_local_names(tree)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and _is_elider_call(n, local_names)
        ]
        # A reference that is not a direct call cannot be followed to its
        # matcher, so it fails loudly instead of vanishing. Checked even when
        # `calls` is empty — a module that ONLY stashes the elider in a dict
        # is exactly the case that would otherwise `continue` past silently.
        _assert_no_opaque_elider_handoff(tree, modname, local_names)
        if not calls:
            continue
        module = importlib.import_module(modname)
        # Statements that both mention the elider's result and do fragment
        # matching. We scan every statement in a scope containing a call for
        # `in` comparisons.
        #
        # `ast.Module` is in the scope list because a call at module scope
        # belongs to no `FunctionDef` and so used to be enumerated into
        # `calls` and then dropped — no haystack audit, no fragment
        # resolution, no floor check. That is a fail-OPEN shape, and it is
        # the fifth one found in this machinery, each a level up from the
        # last.
        #
        # The list is NOT what makes this fail closed, because any allowlist
        # of node types rots the moment Python (or a refactor) introduces a
        # scope kind it does not name. `claimed` below is the actual guard:
        # every call discovered anywhere in the file must end up inside some
        # audited scope, and a call that no scope claims FAILS the suite
        # loudly. A new scope kind therefore cannot go silently unchecked —
        # worst case it is unclaimed, which is a red test, not a green one.
        claimed: set[int] = set()
        scopes = [n for n in ast.walk(tree) if isinstance(n, _AUDITABLE_SCOPES)]
        for func in scopes:
            # INNERMOST enclosing scope only. Scopes nest, so `ast.Module`
            # lexically contains every function in the file; auditing a call
            # in each of its enclosing scopes would re-audit the same call
            # once per level and merge unrelated sites' fragments into one
            # bogus superset row. Keeping only the innermost claimant makes
            # each call audited exactly once, in the scope whose statements
            # actually bind and match it.
            func_calls = [
                c for c in calls
                if _contains(func, c)
                and not any(
                    other is not func
                    and _contains(func, other)
                    and _contains(other, c)
                    for other in scopes
                )
            ]
            if not func_calls:
                continue
            claimed.update(id(c) for c in func_calls)
            # The name the elided reason is bound to (`msg = _elide(...)`).
            # Only comparisons that actually CONSUME that name are fragment
            # matches; scanning every `in` in the function would sweep up
            # unrelated dict-key checks like `"data" in raw`.
            haystacks = {
                t.id
                for c in func_calls
                for a in ast.walk(func)
                if isinstance(a, ast.Assign) and _contains(a.value, c)
                for t in a.targets
                if isinstance(t, ast.Name)
            }
            # Propagate through re-binding: the camconfig site assigns the
            # elided reason to one name and then picks between it and the
            # unelided reason (`msg = elided if elided.strip() else reason`).
            # Anything derived from a haystack is itself a haystack, so the
            # fragments matched against it still count. Iterate to a fixed
            # point so a chain of any length is covered.
            for _ in range(len(list(ast.walk(func)))):
                grown = set(haystacks)
                for a in ast.walk(func):
                    if not isinstance(a, ast.Assign):
                        continue
                    if _names_in(a.value) & haystacks:
                        grown |= {
                            t.id for t in a.targets if isinstance(t, ast.Name)
                        }
                if grown == haystacks:
                    break
                haystacks = grown
            assert haystacks, (
                f"{modname}:{func_calls[0].lineno}: the {ELIDER} result is not "
                "bound to a simple name; cannot identify its matches."
            )
            # Every USE of a haystack must be a shape this test understands.
            # Collecting fragments only from `in`-comparisons made the
            # fail-closed guard below per-SITE instead of per-MATCHER: a
            # sibling `msg.startswith("no bvr")` at a site that also has one
            # `in` check contributed no fragments, resolved to a non-empty
            # `frags` anyway, and was silently exempted from the floor.
            _audit_haystack_uses(
                func, haystacks, modname, func_calls[0].lineno, certified
            )
            # Fragment resolution is per-MATCHER, not per-site. Aggregating
            # into one site-level `frags` let a resolvable matcher vouch for an
            # unresolvable sibling: any single `"literal" in msg` kept `frags`
            # non-empty, so a `for f in list(C)` loop whose container this test
            # cannot read (`_fragments_for_matcher` returns None) contributed
            # nothing and was silently exempted from the floor. Requiring EVERY
            # matcher to resolve its own fragments is what makes the site fail
            # on the one it cannot verify instead of on the aggregate.
            frags: list[str] = []
            matchers = 0
            for cmp_node in ast.walk(func):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not any(isinstance(o, ast.In) for o in cmp_node.ops):
                    continue
                consumes = any(
                    isinstance(n, ast.Name) and n.id in haystacks
                    for c in cmp_node.comparators
                    for n in ast.walk(c)
                )
                if not consumes:
                    continue
                matchers += 1
                resolved = _fragments_for_matcher(func, cmp_node, module)
                assert resolved is not None, (
                    f"{modname}:{cmp_node.lineno}: this `in` matcher consults "
                    "the elided text, but its fragments could not be resolved "
                    "(the needle is not a literal, and its loop container is "
                    "not a module-level name or an inline literal). The floor "
                    "cannot be checked against needles it cannot see, so this "
                    "FAILS rather than leaning on a sibling matcher that did "
                    "resolve — teach `_resolve_fragments` the new shape, or "
                    "hoist the container to a module-level constant."
                )
                frags.extend(resolved)
            # NO `[f for f in frags if f]` filter here. `_resolve_fragments`
            # now returns the EXACT list the runtime iterates, so a filter can
            # only remove signal. It removed exactly one thing: the empty
            # string — a 0-char needle, strictly worse than the 6-char case
            # this suite exists to catch, because `"" in msg` is always True
            # and the matcher is defeated for EVERY message. Dropping it here
            # let the floor pass on a site that classifies every BI failure as
            # camconfig-unavailable / not-a-clip. Empties are now rejected at
            # the two resolvers instead, so nothing reaches this line to
            # filter; were one to, it must arrive at `min(len(f) ...)` and
            # fail the floor loudly rather than be silently discarded.
            assert matchers and frags, (
                f"{modname}:{func_calls[0].lineno}: found an {ELIDER} call but "
                "could not resolve the fragments it gates. This test cannot "
                "verify the site, so it FAILS rather than passing vacuously — "
                "teach _resolve_fragments about the new shape."
            )
            sites.append((modname, func_calls[0].lineno, sorted(set(frags))))
        # Anti-rot backstop. Every call this walker found must have been
        # claimed by an audited scope above. An unclaimed call means its
        # enclosing scope kind is absent from `_AUDITABLE_SCOPES` — exactly
        # the silent drop that hid the module-scope site — so surface it as a
        # failure instead of skipping it. This is what keeps the guard from
        # rotting when a scope kind nobody anticipated appears.
        unclaimed = [c for c in calls if id(c) not in claimed]
        assert not unclaimed, (
            f"{modname}:{unclaimed[0].lineno}: this {ELIDER} call is in a "
            "scope this test does not audit, so its needles never reach the "
            "floor. Add the enclosing node type to `_AUDITABLE_SCOPES` (and "
            "check the fragment resolution understands the new scope), or "
            "move the call into a scope that is already audited."
        )
    return sites


def _fragments_for_matcher(
    func: ast.AST, cmp_node: ast.Compare, module
) -> list[str] | None:
    """Fragments ONE `<needle> in <haystack>` matcher can compare against.

    Returns ``None`` — explicitly "unresolvable" — rather than an empty list
    whenever the needle's provenance cannot be read. The distinction is the
    whole point: `[]` is indistinguishable from "this matcher contributes no
    fragments", which is exactly how an unverifiable matcher used to ride
    along on a resolvable sibling's fragments.
    """
    left = cmp_node.left
    # `"literal" in haystack` — the needle is right there.
    if isinstance(left, ast.Constant):
        return [left.value] if isinstance(left.value, str) else None
    # `frag in haystack`, where `frag` is bound by an enclosing loop.
    if isinstance(left, ast.Name):
        return _fragments_for_loop_var(func, left.id, module)
    # Anything else (a call, an attribute, a subscript) computes the needle at
    # runtime, so its length is not knowable from the AST.
    return None


def _fragments_for_loop_var(
    func: ast.AST, varname: str, module
) -> list[str] | None:
    """Fragments iterated by any loop binding ``varname``, or ``None``.

    Both the comprehension form (`any(f in msg for f in C)`) and the
    statement form (`for f in C: if f in msg`) are resolved. The statement
    form is the same matcher with the comprehension spelled out — de-sugaring
    an `any(...)` is an ordinary refactor, and treating it as an unresolvable
    site would fail the suite on correct code rather than on a real gap.

    ``None`` when no loop binds the name (the needle came from somewhere this
    walk never saw) or when a binding loop's container cannot be read — a
    comprehension, a call like `list(C)`, or a local rebound between its
    definition and its use. Those used to fall out as `[]` and vanish.
    """
    found = False
    out: list[str] = []
    for gen in ast.walk(func):
        if isinstance(gen, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            comps = gen.generators
        elif isinstance(gen, (ast.For, ast.AsyncFor)):
            comps = [gen]
        else:
            continue
        for comp in comps:
            if isinstance(comp.target, ast.Name) and comp.target.id == varname:
                found = True
                resolved = _resolve_fragments(comp.iter, module)
                if resolved is None:
                    return None
                out.extend(resolved)
    return out if found else None


def _contains(outer: ast.AST, target: ast.AST) -> bool:
    return any(n is target for n in ast.walk(outer))


def _resolve_fragments(node: ast.AST, module) -> list[str] | None:
    """String fragments named by ``node``: a module-level container, or
    inline literals. ``None`` when the container cannot be read at all."""
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        # Only the four concrete container types, checked by EXACT type. The
        # whitelist is deliberately narrow because this branch reads a RUNTIME
        # value, and anything outside it cannot be read safely or exactly:
        # a generator is consumed by reading it (this walk would empty the
        # module's own constant); a `dict` iterates keys and a `str` iterates
        # CHARACTERS, neither of which the container's spelling suggests; a
        # custom iterable may populate itself lazily or differ per pass. An
        # exact-type check also excludes subclasses, whose `__iter__` may
        # yield something other than what they store.
        if type(value) in (tuple, list, set, frozenset):
            # Fail CLOSED on any non-`str` element, exactly as
            # `_literal_elements` does. This used to be
            # `[v for v in value if isinstance(v, str)]`, which is the same
            # fail-OPEN shape removed from `_literal_elements`: a filter
            # cannot tell "there was nothing else" from "there was something
            # I could not measure", so a container holding a nested tuple, a
            # `bytes`, or an int silently resolved to its str elements ALONE
            # and the floor was checked against a needle set the runtime code
            # does not iterate. `(("no bvr",), "not bvr", ...)` resolved to
            # shortest-7 while the code really iterates a 6-char needle.
            #
            # This branch matters more than the literal one, not less: TWO
            # of the three real matchers (`tools_cameras:109` and
            # `tools_mutations:816`) iterate a bare module `Name`, so they
            # take THIS path and never reach `_literal_elements`. The third
            # (`tools_mutations:913`) spells its needles inline
            # (`"access denied" in msg or "not authorized" in msg`) and is
            # resolved by `_fragments_for_matcher`'s `ast.Constant` branch,
            # never entering `_resolve_fragments` at all.
            #
            # `type(v) is str` rather than `isinstance`: a `str` SUBCLASS can
            # override `__eq__`/`__contains__`, so its `len()` no longer
            # bounds what it matches — not a fragment whose length this floor
            # can reason about.
            out = []
            for v in value:
                # `not v` alongside the type check: an EMPTY string is a
                # 0-char needle. `"" in msg` is unconditionally True, so it
                # does not merely evade the floor, it defeats the matcher
                # outright for every message. Fail CLOSED at the resolver
                # rather than filtering at the caller — a filter there cannot
                # be seen by a future resolver branch, so the invariant "a
                # resolved fragment is a real, measurable needle" is enforced
                # where fragments are produced.
                if type(v) is not str or not v:
                    return None
                out.append(v)
            return out
        # A local name rebound between definition and use, or a name this
        # module does not expose: unreadable, not empty.
        return None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return _literal_elements(node, module)
    # A comprehension (list/set/dict/generator), a call (`list(C)`,
    # `tuple(C)`, `frozenset(C)`, `C.keys()`), a BinOp (`C + ("x",)`, `C * 2`),
    # an Attribute (`mod.C`), a subscript, a dict display: the iterated values
    # are a runtime property, so the needles are unknowable.
    return None


def _modules_binding_the_elider() -> set[str]:
    """Modules that hold a reference to the elider AT RUNTIME.

    The truth the AST walk is checked against, derived by identity (`is`)
    against the real function object, so it is independent of every spelling:
    an `import ... as` alias, a rebinding, or a plain import all show up as a
    module attribute bound to the same object. The defining module is
    excluded — `client` owns the `def` and does no eliding of its own.
    """
    target = getattr(importlib.import_module("bi_mcp.client"), ELIDER)
    out: set[str] = set()
    for path in _iter_source_files():
        modname = _module_name(path)
        if modname == "bi_mcp.client":
            continue
        module = importlib.import_module(modname)
        if any(v is target for v in vars(module).values()):
            out.add(modname)
    return out


def test_every_module_importing_the_elider_yields_a_site():
    """Positive control, cross-checked against RUNTIME truth.

    This replaces `assert len(sites) >= 3`. A hardcoded count is a magic
    constant of exactly the class this file was built to eliminate: it rots
    the moment a site is legitimately added or removed, and — worse — it is
    nearly blind, firing only if a new spelling REPLACES an existing site
    rather than adding one. That is precisely how an aliased call went
    undetected: the count stayed at 3 and the suite stayed green.

    The expectation is derived instead from the live import graph. Every
    module that actually binds the elider must yield at least one discovered
    site, and the two sides are found by INDEPENDENT means — the left by
    `ast`, the right by object identity at runtime — so a spelling that
    blinds the walker cannot also blind the cross-check. Nothing here needs
    editing when a site is added, moved, or deleted; a module that imports
    the elider and elides nowhere this walk can see is a red test.
    """
    sites = _discover_eliding_sites()
    discovered = {m for m, _, _ in sites}
    binding = _modules_binding_the_elider()
    # The runtime side must itself be non-empty, or the comparison below is
    # vacuous — a broken import graph would otherwise "pass" against zero.
    assert binding, (
        "no module binds the elider at runtime; the cross-check has no truth "
        "to compare against and cannot validate discovery."
    )
    missing = binding - discovered
    assert not missing, (
        f"{sorted(missing)} import `{ELIDER}` at runtime but no eliding site "
        "was discovered there. The call is spelled in a way this walk does "
        "not see, so its fragments never reach the floor — the exact "
        "fail-open this cross-check exists to catch. Teach "
        "`_elider_local_names`/`_is_elider_call` the spelling."
    )
    for _, _, frags in sites:
        assert frags, "a site resolved to zero fragments"


def test_min_elidable_needle_does_not_exceed_shortest_fragment():
    """The constant must be <= the shortest fragment at ANY eliding site."""
    sites = _discover_eliding_sites()
    shortest = min(
        (len(f), f, mod) for mod, _, frags in sites for f in frags
    )
    length, frag, mod = shortest
    assert _MIN_ELIDABLE_NEEDLE <= length, (
        f"_MIN_ELIDABLE_NEEDLE={_MIN_ELIDABLE_NEEDLE} exceeds the shortest "
        f"fragment {frag!r} ({length} chars) at an eliding site in {mod}. A "
        "caller can now plant that fragment as their own text and slip it "
        "past elision. Lower the constant, or lengthen the fragment."
    )


def test_a_shorter_fragment_added_later_is_caught(monkeypatch):
    """Mutation guard: a hypothetical 6-char fragment must FAIL the check.

    Injected into a REAL eliding site's real container, so it exercises the
    same discovery path a genuine future edit would.
    """
    from bi_mcp.tools import tools_cameras

    monkeypatch.setattr(
        tools_cameras,
        "_CAMCONFIG_FALLBACK_FRAGMENTS",
        (*tools_cameras._CAMCONFIG_FALLBACK_FRAGMENTS, "no bvr"),
    )
    with pytest.raises(AssertionError, match="exceeds the shortest fragment"):
        test_min_elidable_needle_does_not_exceed_shortest_fragment()


def test_auth_fail_substrings_are_not_an_eliding_site():
    """`_AUTH_FAIL_SUBSTRINGS` holds a 5-char `login`, which would trip the
    floor — it is excluded only because `_classify_fail` never elides. If
    that ever changes, discovery picks the site up and the floor test fails.
    """
    from bi_mcp.client import _AUTH_FAIL_SUBSTRINGS

    assert min(len(s) for s in _AUTH_FAIL_SUBSTRINGS) < _MIN_ELIDABLE_NEEDLE
    sites = _discover_eliding_sites()
    for mod, _, frags in sites:
        assert set(frags) != set(_AUTH_FAIL_SUBSTRINGS), (
            f"{mod} now elides against the auth substrings; the 5-char "
            "'login' makes the elision guard forgeable there."
        )


def _audit_snippet(body: str) -> str | None:
    """Run the haystack audit over a synthetic function; return its complaint.

    Lets the audit's own contract be tested without mutating real source. The
    snippet is a function body binding the elided text to `reason`.
    """
    source = (
        "def f(e, path):\n"
        "    reason = _elide_caller_text(bi_authored_reason(str(e)), path)\n"
        f"{body}"
    )
    tree = ast.parse(source)
    func = tree.body[0]
    try:
        _audit_haystack_uses(
            func, {"reason"}, "synthetic", 1, _certified_linenos(source)
        )
    except AssertionError as exc:
        return str(exc)
    return None


def test_audit_accepts_shapes_that_cannot_consult_a_needle():
    """Correct, idiomatic code must not trip the audit.

    Each of these consumes the haystack without comparing it to anything, so
    rejecting them would fail the suite on refactors that changed no matcher.
    That over-catching is a real defect: it makes the guard something a
    maintainer routes around rather than maintains.
    """
    assert _audit_snippet('    if reason and "not bvr" in reason:\n        pass\n') is None
    assert _audit_snippet('    _s = f"reason={reason}"\n') is None
    assert _audit_snippet('    _s = reason.strip().lower()\n') is None
    assert _audit_snippet(
        '    log.debug("r=%s", reason)  # needle-free: log arg\n'
    ) is None


def test_audit_still_rejects_every_shape_that_could_hide_a_needle():
    """Fail-closed is the default; the whitelist never widens to a matcher.

    Notably a matcher NESTED inside an accepted shape is still caught: the
    haystack Name then sits under the inner node, which the walk judges on its
    own. Whitelisting an outer shape cannot smuggle an inner one through.
    """
    for body in (
        '    if reason.startswith("no bvr"):\n        pass\n',
        '    if re.search("no bvr", reason):\n        pass\n',
        '    if _is_not_a_clip(reason):\n        pass\n',
        '    if reason == "no bvr":\n        pass\n',
        '    if reason in ("no bvr",):\n        pass\n',
        '    if reason and reason.startswith("no bvr"):\n        pass\n',
        '    _s = f"{reason.startswith(\'no bvr\')}"\n',
        '    if reason.__contains__("no bvr"):\n        pass\n',
        '    if getattr(reason, "startswith")("no bvr"):\n        pass\n',
    ):
        assert _audit_snippet(body) is not None, f"audit let this through: {body!r}"


def test_certification_marker_is_scoped_to_one_line():
    """The escape hatch must certify a single use, not a file or a function.

    If one comment could silence the audit for a whole scope, the original
    defect is simply rebuilt: a later needle-bearing matcher added under the
    same marker would inherit the exemption and never reach the floor.
    """
    # Certified log line AND an uncertified matcher below it: still rejected.
    complaint = _audit_snippet(
        '    log.debug("r=%s", reason)  # needle-free: log arg\n'
        '    if _is_not_a_clip(reason):\n'
        '        pass\n'
    )
    assert complaint is not None and "call" in complaint

    # The marker only excuses calls; it cannot wave through other shapes.
    assert _audit_snippet(
        '    if reason.startswith("no bvr"):  # needle-free: bogus\n'
        '        pass\n'
    ) is not None


def test_marker_must_be_a_real_comment_with_a_reason():
    """The marker is a COMMENT token carrying a `<why>`, not line text.

    Three substring-era abuses, all of which certified for free:

    1. a STRING LITERAL containing the marker — no comment at all, which
       removed the "deliberate untrue comment" cost the hatch is premised on,
       and let an innocent literal quoting the marker disable its line;
    2. a bare `# needle-free:` with no reason, though the failure message
       demands one;
    3. a semicolon-joined line where one honest marker also certified a
       second, needle-bearing use the author never inspected.
    """
    # (1) literal, alone and hiding a real matcher.
    assert _audit_snippet(
        "    log.debug('needle-free: not a comment', reason)\n"
    ) is not None
    assert _audit_snippet(
        '    if re.search("no bvr", reason) and x("needle-free: nope"):\n'
        '        pass\n'
    ) is not None
    # (2) bare marker, no `<why>`.
    assert _audit_snippet('    log.debug("r=%s", reason)  # needle-free:\n') is not None
    # (3) one marker cannot cover two uses on one physical line.
    assert _audit_snippet(
        '    log.debug("%s", reason); y = re.search("no bvr", reason)'
        '  # needle-free: log arg\n'
    ) is not None
    # The honest single use still passes — the hatch must stay usable.
    assert _audit_snippet(
        '    log.debug("r=%s", reason)  # needle-free: logging only\n'
    ) is None


def test_unresolvable_matcher_is_not_vouched_for_by_a_resolvable_sibling():
    """Fragment resolution is per-MATCHER, so masking is impossible.

    `_fragments_for_loop_var` used to return `[]` for a loop whose container
    it could not read (a comprehension, a call, a rebound local). `[]` is
    indistinguishable from "contributed nothing", so any sibling `"literal" in
    msg` kept the site's aggregate non-empty and the unreadable loop's needles
    — of any length — never reached the floor. Unresolvable is now `None`, and
    `None` fails the site regardless of what its siblings resolved.
    """
    import types

    module = types.SimpleNamespace(C=("aaaaaaa", "bbbbbbb"))

    def _matcher(body: str):
        func = ast.parse("def h(msg):\n" + body).body[0]
        cmps = [
            c for c in ast.walk(func)
            if isinstance(c, ast.Compare)
            and any(isinstance(o, ast.In) for o in c.ops)
        ]
        return [_fragments_for_matcher(func, c, module) for c in cmps]

    # Positive control: a readable module-level container still resolves.
    assert _matcher("    for f in C:\n        if f in msg: pass\n") == [
        ["aaaaaaa", "bbbbbbb"]
    ]
    # Every unreadable container reports None, never [].
    for body in (
        "    for f in [c for c in C]:\n        if f in msg: pass\n",
        "    for f in list(C):\n        if f in msg: pass\n",
        "    D = C\n    for f in D:\n        if f in msg: pass\n",
    ):
        assert _matcher(body) == [None], f"masking shape resolved: {body!r}"


def test_certified_matcher_still_fails_when_it_is_the_only_consumer():
    """Defence in depth behind the escape hatch.

    A false certification on a real matcher is the hatch's known limit (see
    `_CERTIFIED_NEEDLE_FREE`). It is not unlimited, though: silencing the
    audit does not conjure fragments. When the certified call is the site's
    only consumer of the elided text, the fragment-resolution assert still
    fails the site, so the hatch alone cannot produce a green run.
    """
    source = (
        "def f(e, path):\n"
        "    reason = _elide_caller_text(bi_authored_reason(str(e)), path)\n"
        "    if _is_not_a_clip(reason):  # needle-free: false claim\n"
        "        pass\n"
    )
    tree = ast.parse(source)
    func = tree.body[0]
    # The audit is silenced by the marker...
    _audit_haystack_uses(
        func, {"reason"}, "synthetic", 1, _certified_linenos(source)
    )
    # ...but no `in` matcher means no fragments, which discovery treats as an
    # unverifiable site rather than a passing one.
    frags = [
        c.left.value
        for c in ast.walk(func)
        if isinstance(c, ast.Compare)
        and any(isinstance(o, ast.In) for o in c.ops)
        and isinstance(c.left, ast.Constant)
    ]
    assert not frags, "expected no resolvable fragments at a certified-only site"
