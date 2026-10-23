"""Tests for services/agent/roles.py — role cards, playbooks, handoff machinery.

Covers:
  - the 13 AUTHORED role cards + 6 playbooks all load and validate (zero errors);
  - allowlists are enforced through the MCP tool_registry (an out-of-scope tool call
    is refused) and are ≤14 tools per role;
  - structured handoffs validate against the card schema and round-trip through the
    design ledger;
  - the context assembler respects the §9.1 injection order and describer budgets;
  - the GBNF emitter produces a grammar that PARSES a valid handoff and rejects a
    malformed one (proven with a self-contained GBNF acceptor — no new dependency);
  - the sketch_profile playbook matches docs/design/ADVISOR_ARCHITECTURE.md §2.4;
  - ``delegate`` and ``workspace_status`` are registered. The org-layer tools
    (channel_send, request_resolve, council_convene, request_raise) are registered
    via services/mcp/org_tools.py. FEATURE_FLAGGED_TOOLS is empty.

Runs headless: no FastMCP, no libchudware, no network, no LLM. The tool catalog is
built by registering the MCP tool modules against a stub (the same pattern
test_tool_registry.py uses).
"""
from __future__ import annotations

import glob
import importlib
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# A local temp dir for throwaway ledgers. We pass explicit ledger paths (below) and
# deliberately do NOT set CHUDWARE_CACHE_ROOT — clobbering that shared env var at import
# time leaks into other modules' tests (e.g. the ledger MCP tools resolve run paths
# from it), so this suite stays hermetic by using explicit paths only.
_TMP_CACHE = tempfile.mkdtemp(prefix="test_roles_cache_")

from services.agent import roles  # noqa: E402
from services.agent.ledger import Ledger  # noqa: E402
from services.mcp import tool_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# Tool catalog (headless): register every MCP tool module against a stub so the
# resolvability check has the full universe of tool names without FastMCP.
# ---------------------------------------------------------------------------

class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[k.get("name") or getattr(fn, "__name__", "?")] = fn
            return fn
        return deco


def _build_catalog() -> set[str]:
    reg.clear_catalog()

    def _regmod(modpath: str, family: str) -> None:
        try:
            m = importlib.import_module(modpath)
        except Exception:  # noqa: BLE001
            return
        for n in dir(m):
            if n.startswith("register_") and n.endswith("_tools"):
                try:
                    getattr(m, n)(reg.view(_StubMCP(), family))
                except Exception:  # noqa: BLE001
                    pass

    for path in sorted(glob.glob(str(_REPO / "services" / "mcp" / "*_tools.py"))):
        mod = os.path.basename(path)[:-3]
        if mod.startswith("test_"):
            continue
        _regmod(f"services.mcp.{mod}", mod.replace("_tools", ""))
    _regmod("services.dag.mcp_tools", "dag")
    _regmod("services.agent.buildgraph.mcp_tools", "buildgraph")
    # cam_audit.py is not named *_tools.py (Wave 2); register it under family cam.
    _regmod("services.mcp.cam_audit", "cam")
    return set(reg.catalog().keys())


_CATALOG = _build_catalog()


def _fresh_ledger(suffix: str = "") -> Ledger:
    run_id = f"test_roles_{suffix}_{os.getpid()}"
    path = Path(_TMP_CACHE) / "runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(run_id, path)


# ---------------------------------------------------------------------------
# A tiny, self-contained GBNF acceptor. Covers exactly the subset roles.py emits:
# rules, `name ::= ...`, alternation `|`, sequence, `?`, `*`, `+`, grouping `( )`,
# string terminals "..." (with \" escapes), char classes [ ... ], rule references.
# It is a backtracking recognizer — not fast, but the handoff JSONs are tiny.
# We use it to PROVE the emitted grammar accepts a valid handoff and rejects a bad
# one, so "GBNF parseable" is demonstrated, not asserted.
# ---------------------------------------------------------------------------

class _Gbnf:
    def __init__(self, grammar: str):
        self.rules: dict[str, str] = {}
        for line in grammar.splitlines():
            line = line.strip()
            if not line or "::=" not in line:
                continue
            name, body = line.split("::=", 1)
            self.rules[name.strip()] = body.strip()

    def accepts(self, text: str, start: str = "root") -> bool:
        # Return True if some parse of `start` consumes the ENTIRE text.
        for end in self._match_rule(start, text, 0):
            if end == len(text):
                return True
        return False

    # Each _match_* is a generator of possible end positions (backtracking).
    def _match_rule(self, name, s, i):
        body = self.rules.get(name)
        if body is None:
            return
        yield from self._match_alt(body, s, i)

    def _match_alt(self, expr, s, i):
        for branch in _split_top(expr, "|"):
            yield from self._match_seq(branch.strip(), s, i)

    def _match_seq(self, expr, s, i):
        tokens = _tokenize(expr)
        yield from self._match_tokens(tokens, 0, s, i)

    def _match_tokens(self, tokens, ti, s, i):
        if ti >= len(tokens):
            yield i
            return
        tok = tokens[ti]
        for j in self._match_token(tok, s, i):
            yield from self._match_tokens(tokens, ti + 1, s, j)

    def _match_token(self, tok, s, i):
        # Repetition suffixes. A quoted literal always ends with '"', so a token
        # ending in * or ? is a suffix — including an OPTIONAL quoted literal like
        # `"-"?` (used by the integer/number rules); the old `not startswith('"')`
        # guard wrongly excluded that case and broke integer handoffs.
        if tok.endswith("*") or tok.endswith("+"):
            plus = tok.endswith("+")
            base = tok[:-1]
            # zero-or-more (one-or-more for `+`, used by the number primitive's
            # [0-9]+): collect all reachable positions (greedy-with-backtrack).
            seen = {i}
            frontier = [i]
            results = [] if plus else [i]
            while frontier:
                nxt = []
                for pos in frontier:
                    for e in self._match_atom(base, s, pos):
                        if e not in seen:
                            seen.add(e)
                            nxt.append(e)
                            results.append(e)
                frontier = nxt
            yield from results
            return
        if tok.endswith("?"):
            base = tok[:-1]
            yield i
            yield from self._match_atom(base, s, i)
            return
        yield from self._match_atom(tok, s, i)

    def _match_atom(self, tok, s, i):
        if not tok:
            yield i
            return
        if tok.startswith("(") and tok.endswith(")"):
            yield from self._match_alt(tok[1:-1].strip(), s, i)
            return
        if tok.startswith('"'):
            lit = _unquote(tok)
            if s.startswith(lit, i):
                yield i + len(lit)
            return
        if tok.startswith("["):
            # character class, single char
            if i < len(s) and _class_match(tok, s[i]):
                yield i + 1
            return
        # rule reference (possibly with * / ? already stripped by caller)
        yield from self._match_rule(tok, s, i)


def _split_top(expr, sep):
    """Split on `sep` at the top level (not inside quotes / brackets / parens).

    Tracks a char-class ([...]) separately so a `"` inside a class (e.g. [^"\\])
    does not toggle string state."""
    out, depth, i, start = [], 0, 0, 0
    in_str = in_class = False
    while i < len(expr):
        c = expr[i]
        if in_class:
            if c == "]":
                in_class = False
        elif in_str:
            if c == '"' and expr[i - 1] != "\\":
                in_str = False
        else:
            if c == "[":
                in_class = True
            elif c == '"':
                in_str = True
            elif c in "(":
                depth += 1
            elif c in ")":
                depth -= 1
            elif c == sep and depth == 0:
                out.append(expr[start:i])
                start = i + 1
        i += 1
    out.append(expr[start:])
    return out


def _tokenize(expr):
    """Split a sequence into atoms, honoring quotes, [ ], ( ), and */? suffixes."""
    toks, i, n = [], 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and not (expr[j] == '"' and expr[j - 1] != "\\"):
                j += 1
            tok = expr[i:j + 1]
            i = j + 1
        elif c == "[":
            j = expr.index("]", i)
            tok = expr[i:j + 1]
            i = j + 1
        elif c == "(":
            depth, j = 1, i + 1
            while j < n and depth:
                if expr[j] == "(":
                    depth += 1
                elif expr[j] == ")":
                    depth -= 1
                j += 1
            tok = expr[i:j]
            i = j
        else:
            j = i
            while j < n and not expr[j].isspace() and expr[j] not in '("[':
                j += 1
            tok = expr[i:j]
            i = j
        # attach a trailing *, ?, or + that follows the atom
        if i < n and expr[i] in "*?+":
            tok += expr[i]
            i += 1
        toks.append(tok)
    return toks


def _unquote(tok):
    inner = tok[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\").replace("\\t", "\t").replace("\\n", "\n")


def _class_match(tok, ch):
    body = tok[1:-1]
    negate = False
    if body.startswith("^"):
        negate = True
        body = body[1:]
    # handle escapes like \t \n and ranges a-z
    i, items, ranges = 0, set(), []
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            items.add({"t": "\t", "n": "\n", "\\": "\\", '"': '"'}.get(nxt, nxt))
            i += 2
            continue
        if i + 2 < len(body) and body[i + 1] == "-":
            ranges.append((c, body[i + 2]))
            i += 3
            continue
        items.add(c)
        i += 1
    hit = ch in items or any(a <= ch <= b for a, b in ranges)
    return (not hit) if negate else hit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoadAndValidate(unittest.TestCase):
    def test_all_roles_load(self):
        # 13 pipeline roles + department_manager + design_thinker (org/ladder
        # layers) + feature_architect (design-pipeline Stage 1) +
        # geometry_designer (design-pipeline Stage 2) +
        # linkage_planner (design-pipeline Stage 3) +
        # sourcing_specialist (Parts Library V2).
        # council_seats.yaml is data, not a card.
        cards = roles.load_all_roles()
        self.assertGreaterEqual(len(cards), 19, sorted(cards))
        self.assertIn("sketch2d", cards)
        self.assertIn("model3d", cards)
        for rid in ("intake_analyst", "clarifier", "spec_writer", "orchestrator",
                    "layout_engineer", "sketcher", "feature_modeler",
                    "assembly_fit_engineer", "sim_engineer", "cam_programmer",
                    "reviewer", "fixer", "overseer",
                    "department_manager", "design_thinker",
                    "feature_architect", "geometry_designer",
                    "linkage_planner", "sourcing_specialist"):
            self.assertIn(rid, cards)

    def test_six_playbooks_load(self):
        pbs = roles.load_all_playbooks()
        self.assertGreaterEqual(len(pbs), 6)
        for pid in ("sketch_profile", "feature_sequence", "layout_first",
                    "assembly_fit", "sim_study", "cam_job"):
            self.assertIn(pid, pbs)

    def test_suite_has_zero_errors(self):
        findings = roles.validate_suite(tool_names=_CATALOG)
        errs = roles.errors(findings)
        self.assertEqual(errs, [], "\n".join(str(f) for f in errs))

    def test_only_expected_feature_flags(self):
        findings = roles.validate_suite(tool_names=_CATALOG)
        flagged = {f.message.split("'")[1] for f in roles.flags(findings)
                   if "not yet registered" in f.message}
        # delegate and workspace_status are registered; channel_send,
        # request_resolve, council_convene are registered via org_tools.
        # All org-layer tools now registered → empty set expected.
        self.assertEqual(flagged, set())

    def test_allowlists_within_cap(self):
        for card in roles.load_all_roles().values():
            self.assertLessEqual(len(card.tool_allowlist), roles.MAX_TOOLS_PER_ROLE,
                                 f"{card.id} exceeds tool cap")

    def test_stage_scopes_in_vocabulary(self):
        for card in roles.load_all_roles().values():
            for st in card.stage_scope:
                self.assertIn(st, roles.STAGES, f"{card.id}: {st}")

    def test_playbook_defaults_resolve(self):
        cards = roles.load_all_roles()
        pbs = roles.load_all_playbooks()
        for card in cards.values():
            if card.playbook_default:
                self.assertIn(card.playbook_default, pbs, card.id)

    def test_bad_role_schema_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text("schema: aicad.role/2\nid: bad\n")
            with self.assertRaises(roles.RoleError):
                roles.load_role("bad", roles_dir=Path(d))


class TestSketchProfileVerbatim(unittest.TestCase):
    """sketch_profile must match docs/design/ADVISOR_ARCHITECTURE.md §2.4."""

    def test_steps_and_helpers(self):
        pb = roles.load_playbook("sketch_profile")
        self.assertEqual(pb.stage, "sketch")
        step_ids = [s.id for s in pb.steps]
        # §2.4: datums, entities, close, geometric constraints, driving dims, exit gate
        self.assertEqual(step_ids[:5],
                         ["datums", "entities", "close",
                          "geometric_constraints", "driving_dimensions"])
        self.assertEqual(pb.exit_gate,
                         ("sketch_fully_constrained", "profile_closed",
                          "no_conflicting_tags"))
        # helper tool names from the §2.4 spec / sketch-tool contracts
        helpers = {h for s in pb.steps for h in s.helper_tools}
        self.assertIn("sketch_health", helpers)
        self.assertIn("suggest_dimension_scheme", helpers)

    def test_helpers_subset_of_sketcher_allowlist(self):
        pb = roles.load_playbook("sketch_profile")
        sketcher = roles.load_role("sketcher")
        allow = set(sketcher.concrete_tools)
        for s in pb.steps:
            for h in s.helper_tools:
                self.assertIn(h, allow, f"{s.id}: {h} not in sketcher allowlist")


class TestAllowlistEnforcement(unittest.TestCase):
    def setUp(self):
        reg.reset_policy()

    def tearDown(self):
        reg.reset_policy()

    def test_out_of_scope_tool_blocked(self):
        sketcher = roles.load_role("sketcher")
        sketcher.apply_tool_policy(reg)
        # in-scope tool allowed
        self.assertTrue(reg.is_allowed("sketch_health"))
        # out-of-scope tool refused (cam is not in the sketcher card)
        self.assertFalse(reg.is_allowed("cam_post"))
        refusal = reg.refusal("cam_post")
        self.assertFalse(refusal["ok"])
        self.assertIn("allow-list", refusal["reason"])

    def test_guard_returns_refusal_on_dispatch(self):
        """A guarded tool function returns the refusal dict instead of running when
        the tool is outside the active role allowlist."""
        stub = _StubMCP()
        view = reg.view(stub, "cam")

        @view.tool()
        def cam_post():  # registered under family cam
            return {"ok": True, "posted": True}

        roles.load_role("sketcher").apply_tool_policy(reg)
        out = stub.tools["cam_post"]()
        self.assertFalse(out["ok"])  # refused, did not run


class TestHandoffRoundTrip(unittest.TestCase):
    def test_valid_handoff_validates_and_persists(self):
        card = roles.load_role("sketcher")
        env = roles.make_handoff(
            card,
            status="done",
            summary="closed, DOF=0",
            outputs={
                "sketch_ref": {"doc_id": "d1", "sketch_id": "s1"},
                "health": {"closed": True, "dof": 0, "conflicts": []},
                "driving_dimensions": [
                    {"name": "bore_d", "value": 20.02, "ledger_dim_id": "DIM1"}
                ],
                "reasoning_summary": "constrained geometry then dimensions",
            },
        )
        self.assertEqual(roles.validate_handoff(card, env), [])
        led = _fresh_ledger("handoff")
        evt = roles.append_handoff_to_ledger(led, card, env)
        self.assertEqual(evt["kind"], "handoff")
        self.assertEqual(evt["actor"], "sketcher")
        # round-trip: reload the ledger from disk and find the handoff event
        led2 = Ledger.load(led.run_id, led.path)
        handoffs = [e for e in led2.events if e["kind"] == "handoff"]
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["payload"]["outputs"]["sketch_ref"]["doc_id"], "d1")

    def test_missing_required_output_rejected(self):
        card = roles.load_role("sketcher")
        env = roles.make_handoff(card, status="done", summary="x",
                                 outputs={"sketch_ref": {"doc_id": "d", "sketch_id": "s"}})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("health" in e for e in errs), errs)

    def test_bad_status_rejected(self):
        card = roles.load_role("sketcher")
        env = roles.make_handoff(card, status="finished", summary="x", outputs={})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("status" in e for e in errs), errs)

    def test_maxlength_enforced_post_parse(self):
        card = roles.load_role("clarifier")  # summary maxLength 500
        env = roles.make_handoff(
            card, status="done", summary="x" * 600,
            outputs={"resolved": [], "still_open": [], "reasoning_summary": "ok"})
        errs = roles.validate_handoff(card, env)
        self.assertTrue(any("maxLength" in e for e in errs), errs)

    def test_invalid_handoff_not_persisted(self):
        card = roles.load_role("sketcher")
        led = _fresh_ledger("bad")
        env = roles.make_handoff(card, status="nope", summary="x", outputs={})
        with self.assertRaises(roles.RoleError):
            roles.append_handoff_to_ledger(led, card, env)
        self.assertEqual([e for e in led.events if e["kind"] == "handoff"], [])


class TestContextAssembler(unittest.TestCase):
    def test_injection_order(self):
        card = roles.load_role("sketcher")
        # Use sentinels that cannot collide with prompt text (the sketcher prompt
        # itself mentions "[ADVISORY]", so plain words would find the wrong index).
        ctx = roles.assemble_context(
            card,
            ledger_render="§§LEDGER§§",
            stage_state="§§STATE§§",
            advisory_block="§§ADV§§",
            playbook_step="§§PLAYBOOK§§",
            history="§§HISTORY§§",
        )
        # §9.1: system prompt → ledger → stage state → advisory → playbook → history
        idx = [ctx.index(tok) for tok in
               (card.system_prompt.strip()[:20], "§§LEDGER§§", "§§STATE§§",
                "§§ADV§§", "§§PLAYBOOK§§", "§§HISTORY§§")]
        self.assertEqual(idx, sorted(idx))

    def test_empty_slots_omitted_order_preserved(self):
        card = roles.load_role("sketcher")
        ctx = roles.assemble_context(card, ledger_render="§§LEDGER§§",
                                     history="§§HISTORY§§")
        self.assertLess(ctx.index("§§LEDGER§§"), ctx.index("§§HISTORY§§"))
        self.assertNotIn("§§ADV§§", ctx)

    def test_describer_budgets_passed(self):
        card = roles.load_role("sketcher")
        seen = []

        def describer(tool, budget):
            seen.append((tool, budget))
            return f"{tool}@{budget}"

        state = roles.render_stage_state(card, describer=describer)
        # sketcher recipe: describe_sketch budget 1200
        self.assertEqual(seen, [("describe_sketch", 1200)])
        self.assertIn("describe_sketch@1200", state)

    def test_no_describer_headless(self):
        card = roles.load_role("sketcher")
        self.assertEqual(roles.render_stage_state(card, describer=None), "")


class TestGbnfEmitter(unittest.TestCase):
    def test_grammar_parses_valid_handoff_for_every_role(self):
        for card in roles.load_all_roles().values():
            grammar = roles.schema_to_gbnf(card.handoff_schema)
            acceptor = _Gbnf(grammar)
            valid = _minimal_valid_json(card.handoff_schema)
            self.assertTrue(acceptor.accepts(valid),
                            f"{card.id}: grammar rejected a valid handoff:\n{valid}")

    def test_grammar_rejects_malformed(self):
        card = roles.load_role("sketcher")
        acceptor = _Gbnf(roles.schema_to_gbnf(card.handoff_schema))
        # missing braces / not an object
        self.assertFalse(acceptor.accepts('"just a string"'))
        self.assertFalse(acceptor.accepts("{"))

    def test_enum_literals_constrained(self):
        # status enum must appear as string literals in the grammar
        card = roles.load_role("sketcher")
        grammar = roles.schema_to_gbnf(card.handoff_schema)
        self.assertIn('\\"done\\"', grammar)
        self.assertIn('\\"blocked\\"', grammar)

    def test_ref_does_not_crash(self):
        # intake_analyst.outputs.manifest is a $ref (aicad.deliverables/1)
        card = roles.load_role("intake_analyst")
        grammar = roles.schema_to_gbnf(card.handoff_schema)  # must not raise
        self.assertTrue(grammar.startswith("root ::="))

    def test_grammar_permits_optional_replan_request(self):
        """The GBNF must PERMIT a non-required output like replan_request — every
        worker prompt can emit one (the anti-grind valve, contract §3.7/§17). A
        grammar that only allowed required keys would make replan impossible on the
        exact (local) tier the grammar exists for."""
        for rid in ("spec_writer", "sketcher", "feature_modeler", "layout_engineer",
                    "sim_engineer", "cam_programmer", "fixer"):
            card = roles.load_role(rid)
            self.assertIn("replan_request",
                          card.handoff_schema["properties"]["outputs"].get("properties", {}),
                          rid)
            acceptor = _Gbnf(roles.schema_to_gbnf(card.handoff_schema))
            handoff = _with_replan(card.handoff_schema)
            import json as _json
            self.assertTrue(acceptor.accepts(_json.dumps(handoff)),
                            f"{rid}: grammar rejected a replan-bearing handoff")

    def test_required_keys_still_enforced(self):
        """The optional-members tail must not let a required key be dropped."""
        card = roles.load_role("sketcher")
        acceptor = _Gbnf(roles.schema_to_gbnf(card.handoff_schema))
        # outputs is required at the top level — an envelope missing it is rejected
        self.assertFalse(acceptor.accepts('{"status": "done", "summary": "x"}'))


# ---------------------------------------------------------------------------
# helper: build a minimal valid JSON string for a handoff schema (for GBNF proof)
# ---------------------------------------------------------------------------

def _minimal_valid_json(schema, depth=0):
    import json as _json
    return _json.dumps(_minimal_value(schema, depth))


def _with_replan(schema):
    """A minimal valid handoff value for ``schema`` with a replan_request object added
    to outputs (an optional member the grammar must permit)."""
    val = _minimal_value(schema)
    outputs = val.get("outputs")
    if isinstance(outputs, dict):
        outputs["replan_request"] = {
            "schema": "aicad.replan/1", "scope": "approach",
            "reason": "two failures, better route", "urgency": "opportunistic",
        }
    return val


def _minimal_value(schema, depth=0):
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return {}  # opaque object
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = t[0]
    if t == "object":
        obj = {}
        for k in schema.get("required", []):
            sub = schema.get("properties", {}).get(k, {})
            obj[k] = _minimal_value(sub, depth + 1)
        return obj
    if t == "array":
        return []
    if t == "string":
        return "x"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "null":
        return None
    return {}


class TestSketch2dModel3dRoles(unittest.TestCase):
    """New 2D/3D split roles (sketch2d + model3d)."""

    def test_load_sketch2d_and_model3d(self):
        s2 = roles.load_role("sketch2d")
        m3 = roles.load_role("model3d")
        self.assertEqual(s2.id, "sketch2d")
        self.assertEqual(m3.id, "model3d")
        self.assertLessEqual(len(s2.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)
        self.assertLessEqual(len(m3.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)

    def test_model3d_prompt_contract(self):
        m3 = roles.load_role("model3d")
        prompt = m3.system_prompt
        low = prompt.lower()
        self.assertIn("delegate", m3.concrete_tools)
        self.assertIn("sketch2d", low)
        self.assertIn("vision", low)
        self.assertIn("delegate", low)

    def test_sketch2d_no_solids_contract(self):
        s2 = roles.load_role("sketch2d")
        low = s2.system_prompt.lower()
        self.assertIn("do not", low)
        self.assertIn("3d solids", low)
        self.assertNotIn("delegate", s2.concrete_tools)
        # 2D tools only — no pure solid planners beyond sketch-capable add_features
        self.assertIn("sketch_health", s2.concrete_tools)

    def test_roles_validate_against_catalog(self):
        for rid in ("sketch2d", "model3d"):
            card = roles.load_role(rid)
            findings = roles.validate_role(card, tool_names=_CATALOG)
            errs = [f for f in findings if f.level == "error"]
            self.assertEqual(errs, [], f"{rid}: " + "; ".join(str(e) for e in errs))



class TestPromptOverrideIsolation(unittest.TestCase):
    def test_mission_and_native_overrides_with_same_id_do_not_collide(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"CHUDWARE_APPDATA": td}
        ):
            prompt_dir = Path(td) / "prompts"
            prompt_dir.mkdir(parents=True)
            core_override = prompt_dir / "reviewer.md"
            mission_override = prompt_dir / "reviewer.mission.md"
            core_override.write_text(
                "CORE REVIEWER OUTPUT: {done, issues}", encoding="utf-8"
            )
            mission_override.write_text(
                "MISSION REVIEWER OUTPUT: criterion_results", encoding="utf-8"
            )

            self.assertEqual(
                roles.resolve_role_system_prompt("reviewer", "YAML FALLBACK"),
                "MISSION REVIEWER OUTPUT: criterion_results",
            )
            rows = {row["role"]: row for row in roles.list_editable_prompts()}
            self.assertEqual(
                rows["mission:reviewer"]["override_path"], str(mission_override)
            )
            self.assertEqual(
                rows["core:reviewer"]["override_path"], str(core_override)
            )
            self.assertIn("criterion_results", rows["mission:reviewer"]["text"])
            self.assertIn("{done, issues}", rows["core:reviewer"]["text"])

    def test_role_guidance_uses_real_meta_and_datasheet_call_shapes(self):
        prompts = {
            role_id: card.system_prompt
            for role_id, card in roles.load_all_roles().items()
        }
        self.assertNotIn("enable_tools([", "\n".join(prompts.values()))
        datasheet = prompts["re_datasheet_reader"]
        self.assertIn("research_datasheet_fetch(source=<PDF URL>)", datasheet)
        self.assertNotIn("research_datasheet_fetch(mpn)", datasheet)

if __name__ == "__main__":
    unittest.main()


def test_role_prompts_do_not_delegate_to_unknown_roles():
    roles_dir = _REPO / "services" / "agent" / "roles"
    known = {
        line.split(":", 1)[1].strip()
        for path in roles_dir.glob("*.yaml")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("id:")
    }
    referenced: set[str] = set()
    for root in (roles_dir, _REPO / "services" / "agent" / "prompts"):
        for path in root.glob("*.yaml" if root == roles_dir else "*.md"):
            text = path.read_text(encoding="utf-8")
            referenced.update(re.findall(r"delegate\(role=[\"']([^\"']+)", text))
    assert referenced <= known, f"role prompts delegate to unknown roles: {referenced - known}"
