"""Unit tests for the SCORING layer — canned turn results, no live model, no engine.

Every test asserts a NUMBER or a specific classification, because the failure mode this
suite exists to prevent is a scorer that quietly says PASS. The nastiest cases here are:

  * a criterion that would pass because the check never ran (``ledger_zero``),
  * a criterion that would pass on the model's prose instead of the engine payload,
  * a criterion that would pass because the tool result arrived as a JSON *string*,
  * a harness defect misfiled as a model error (the classification that decides whether
    anyone in this repo has work to do).

Run:  .venv-mcp/bin/python -m pytest benchmarks/artifact_validity/test_scoring.py -q
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from benchmarks.artifact_validity.prompts import PROMPTS, Prompt  # noqa: E402
from benchmarks.artifact_validity.scoring import (  # noqa: E402
    CLASS_HARNESS, CLASS_MODEL, CLASS_REFUSAL, classify_call, classify_prompt,
    collect_views, diff_reports, evaluate_criterion, format_report, payload, score_run,
)


# ------------------------------------------------------------------ canned data helpers
def call(tool: str, result, ok: bool = True, args: dict | None = None) -> dict:
    return {"tool": tool, "args": args or {}, "ok": ok, "result": result}


def run(prompt_id: str = "p", calls: list[dict] | None = None, **kw) -> dict:
    base = {"prompt_id": prompt_id, "prompt": "x", "success": True,
            "stop_reason": "confirmed", "elapsed_s": 1.0, "calls": calls or []}
    base.update(kw)
    return base


def part(volume: float, *, watertight: bool = True, free: int = 0, nonman: int = 0,
         bbox=None, path: str = "/w/a.step+") -> dict:
    return {"path": path, "opened": True, "watertight": watertight, "free_edges": free,
            "non_manifold_edges": nonman, "volume_mm3": volume,
            "bbox_size_mm": bbox, "bodies": 1}


def ev(criterion: dict, r: dict, parts=(), files=()) -> dict:
    return evaluate_criterion(criterion, r, list(parts), list(files))


# ============================================================== artifact-probe criteria
class TestPartCriteria:
    def test_watertight_solid_passes_and_reports_its_numbers(self):
        got = ev({"kind": "part_health"}, run(), [part(53057.5)])
        assert got["passed"] is True
        assert "watertight=True" in got["observed"]
        assert "53057.5" in got["observed"]

    def test_free_edges_fail_even_when_watertight_flag_is_true(self):
        """A body with boundary edges is not sound however the flag reads."""
        got = ev({"kind": "part_health"}, run(), [part(100.0, free=4)])
        assert got["passed"] is False
        assert "free_edges=4" in got["observed"]

    def test_non_manifold_edges_fail(self):
        assert ev({"kind": "part_health"}, run(), [part(100.0, nonman=2)])["passed"] is False

    def test_zero_volume_body_is_not_a_solid(self):
        got = ev({"kind": "part_health"}, run(), [part(0.0)])
        assert got["passed"] is False
        assert "no solid part on disk" in got["observed"]

    def test_no_artifact_at_all_fails_with_the_probe_count(self):
        got = ev({"kind": "part_health"}, run(), [])
        assert got["passed"] is False and "probed 0 doc(s)" in got["observed"]

    def test_volume_inside_tolerance_passes_and_prints_the_error(self):
        want = 53057.52
        got = ev({"kind": "part_volume", "expect_mm3": want, "tol_pct": 2.0},
                 run(), [part(want * 1.019)])
        assert got["passed"] is True
        assert "1.90% off" in got["observed"]

    def test_volume_outside_tolerance_fails(self):
        want = 53057.52
        got = ev({"kind": "part_volume", "expect_mm3": want, "tol_pct": 2.0},
                 run(), [part(want * 1.021)])
        assert got["passed"] is False and "2.10% off" in got["observed"]

    def test_solid_plate_without_the_hole_fails_volume(self):
        """The whole point of the volume criterion: a plate that skipped the hole is
        watertight, right-sized and WRONG. 54000 vs 53057.5 is 1.78%... so the hole
        criterion must be tight enough to see it."""
        got = ev({"kind": "part_volume", "expect_mm3": 53057.52, "tol_pct": 1.0},
                 run(), [part(54000.0)])
        assert got["passed"] is False

    def test_best_part_is_chosen_by_volume_not_by_order(self):
        """A turn leaves intermediates on disk; the criterion must find the right doc."""
        parts = [part(1.0, path="/w/scratch.step+"), part(53057.5, path="/w/plate.step+")]
        got = ev({"kind": "part_volume", "expect_mm3": 53057.52, "tol_pct": 1.0},
                 run(), parts)
        assert got["passed"] is True and "plate.step+" in got["observed"]

    def test_bbox_is_orientation_insensitive_but_size_sensitive(self):
        c = {"kind": "part_bbox", "size_mm": [90.0, 50.0, 12.0], "tol_mm": 0.5}
        assert ev(c, run(), [part(1.0, bbox=[12.0, 90.0, 50.0])])["passed"] is True
        got = ev(c, run(), [part(1.0, bbox=[90.0, 50.0, 14.0])])
        assert got["passed"] is False and "2.000 mm" in got["observed"]

    def test_missing_bbox_fails_rather_than_passing_by_default(self):
        got = ev({"kind": "part_bbox", "size_mm": [1, 2, 3], "tol_mm": 0.5},
                 run(), [part(1.0, bbox=None)])
        assert got["passed"] is False


class TestFileCriteria:
    def test_pdf_with_pages_passes(self):
        got = ev({"kind": "file_present", "glob": "*.pdf", "min_pages": 1}, run(), [],
                 [{"path": "/w/d.pdf", "name": "d.pdf", "bytes": 4096, "pages": 2}])
        assert got["passed"] is True and "2 page(s)" in got["observed"]

    def test_unreadable_page_count_fails_it_is_not_a_pdf_we_can_vouch_for(self):
        got = ev({"kind": "file_present", "glob": "*.pdf", "min_pages": 1}, run(), [],
                 [{"path": "/w/d.pdf", "name": "d.pdf", "bytes": 9, "pages": None}])
        assert got["passed"] is False and "unreadable" in got["observed"]

    def test_absent_file_lists_what_was_there(self):
        got = ev({"kind": "file_present", "glob": "*.pdf", "min_pages": 1}, run(), [],
                 [{"path": "/w/a.step+", "name": "a.step+", "bytes": 10, "pages": None}])
        assert got["passed"] is False and "a.step+" in got["observed"]


# ================================================================ ledger (engine payload)
class TestPayloadPlumbing:
    def test_double_encoded_json_string_result_is_decoded(self):
        c = call("mass_properties", json.dumps({"ok": True, "mass_g": 583.2}))
        assert payload(c)["mass_g"] == 583.2

    def test_plain_dict_result_is_passed_through(self):
        assert payload(call("t", {"mass_g": 1.0}))["mass_g"] == 1.0

    def test_non_json_string_survives_as_text(self):
        assert payload(call("t", "just prose")) == "just prose"


class TestLedgerCriteria:
    NUM = {"kind": "ledger_number", "tools": ("fea_static",),
           "keys": ("max_von_mises_mpa", "max_von_mises"), "min": 150.0, "max": 480.0,
           "quantity": "stress"}

    def test_engine_value_in_band_passes_with_the_value_quoted(self):
        got = ev(self.NUM, run(calls=[call("fea_static", {"max_von_mises_mpa": 281.0})]))
        assert got["passed"] is True and "281" in got["observed"]

    def test_engine_value_out_of_band_fails(self):
        got = ev(self.NUM, run(calls=[call("fea_static", {"max_von_mises_mpa": 12.0})]))
        assert got["passed"] is False and "outside" in got["observed"]

    def test_unit_ambiguous_key_is_tried_both_ways_and_says_which_landed(self):
        """max_von_mises with no unit suffix could be Pa or MPa; 2.81e8 Pa is the same
        answer as 281 MPa and must not be scored as an out-of-band failure."""
        got = ev(self.NUM, run(calls=[call("fea_static", {"max_von_mises": 2.81e8})]))
        assert got["passed"] is True and "Pa->MPa" in got["observed"]

    def test_value_from_a_FAILED_call_cannot_satisfy_a_criterion(self):
        got = ev(self.NUM, run(calls=[call("fea_static", {"max_von_mises_mpa": 281.0},
                                           ok=False)]))
        assert got["passed"] is False and "no engine value" in got["observed"]

    def test_value_from_the_wrong_tool_is_ignored(self):
        got = ev(self.NUM, run(calls=[call("some_other_tool",
                                           {"max_von_mises_mpa": 281.0})]))
        assert got["passed"] is False

    def test_model_prose_claiming_the_number_does_not_pass(self):
        """The whole thesis of the benchmark: prose is not evidence."""
        r = run(calls=[], reply="Peak von Mises is 281 MPa and the tip deflects 1.7 mm.")
        assert ev(self.NUM, r)["passed"] is False

    def test_nested_engine_value_is_found(self):
        r = run(calls=[call("fea_static", {"ok": True,
                                           "results": {"summary": {"max_von_mises_mpa": 300}}})])
        assert ev(self.NUM, r)["passed"] is True

    def test_zero_criterion_fails_when_the_check_never_ran(self):
        """'DRC is clean' and 'nobody ran DRC' must not score the same. This is the single
        easiest way for a benchmark to report a fake pass."""
        c = {"kind": "ledger_zero", "tools": ("ecad_run_drc",), "keys": ("errors",)}
        got = ev(c, run(calls=[call("ecad_autoroute", {"routed": 4})]))
        assert got["passed"] is False and "did not run" in got["observed"]

    def test_zero_criterion_passes_only_on_a_reported_zero(self):
        c = {"kind": "ledger_zero", "tools": ("ecad_run_drc",), "keys": ("errors",)}
        assert ev(c, run(calls=[call("ecad_run_drc", {"errors": 0})]))["passed"] is True
        got = ev(c, run(calls=[call("ecad_run_drc", {"errors": 3})]))
        assert got["passed"] is False and "= 3" in got["observed"]

    def test_zero_criterion_takes_the_WORST_report_not_the_last(self):
        """A clean re-run after a dirty one must not launder the dirty one."""
        c = {"kind": "ledger_zero", "tools": ("ecad_run_drc",), "keys": ("errors",)}
        r = run(calls=[call("ecad_run_drc", {"errors": 5}), call("ecad_run_drc", {"errors": 0})])
        assert ev(c, r)["passed"] is False

    def test_min_criterion_uses_the_best_report(self):
        c = {"kind": "ledger_min", "tools": ("ecad_status",), "keys": ("nets",), "min": 3}
        r = run(calls=[call("ecad_status", {"nets": 0}), call("ecad_status", {"nets": 4})])
        got = ev(c, r)
        assert got["passed"] is True and "= 4" in got["observed"]

    def test_near_criterion_reports_the_closest_miss(self):
        c = {"kind": "ledger_near", "tools": ("drawing_add_dim",), "keys": ("value",),
             "expect": 10.0, "tol": 0.05}
        assert ev(c, run(calls=[call("drawing_add_dim", {"value": 10.0})]))["passed"] is True
        got = ev(c, run(calls=[call("drawing_add_dim", {"value": 12.0})]))
        assert got["passed"] is False and "closest" in got["observed"] and "12" in got["observed"]

    def test_dimension_value_comes_from_the_engine_not_from_the_args(self):
        """Verbatim add_dimension reply from a live run: the model asked for a='f2',
        dtype='diameter' and the ENGINE resolved and measured the projected circle. The
        10.0 is the engine's model_diameter, which is why this criterion is evidence."""
        c = {"kind": "ledger_near", "tools": ("add_dimension",), "keys": ("value",),
             "expect": 10.0, "tol": 0.05}
        real = call("add_dimension", json.dumps({
            "ok": True, "annotation": "a4", "dtype": "diameter_dim", "value": 10.0,
            "text": "\u230010",
            "resolved": {"a": {"from": "f2", "sheet_point": [148.5, 150.5],
                               "model_diameter": 10.0}}}),
            args={"doc_id": "dwg-cpp00000001", "view_id": "v2", "a": "f2",
                  "dtype": "diameter", "pos": [95, 60]})
        assert ev(c, run(calls=[real]))["passed"] is True
        # The same request against a 12 mm hole must fail: only the engine's number moves.
        wrong = dict(real, result=json.dumps({"ok": True, "value": 12.0}))
        assert ev(c, run(calls=[wrong]))["passed"] is False

    def test_count_criterion_measures_a_LIST_length(self):
        """ecad_autoroute reports routed_nets as a list of nets, not an integer. A
        numeric-only reader scores a fully routed board as unrouted."""
        c = {"kind": "ledger_count", "tools": ("ecad_autoroute",),
             "keys": ("routed_nets",), "min": 1}
        got = ev(c, run(calls=[call("ecad_autoroute",
                                    {"ok": True, "routed_nets": ["GND", "VCC"]})]))
        assert got["passed"] is True and "counts 2" in got["observed"]
        empty = ev(c, run(calls=[call("ecad_autoroute", {"ok": True, "routed_nets": []})]))
        assert empty["passed"] is False and "counts 0" in empty["observed"]

    def test_distinct_criterion_counts_engine_assigned_refs_not_calls(self):
        """Three place_symbol calls that all landed on U1 is ONE component, not three."""
        c = {"kind": "ledger_distinct", "tools": ("ecad_place_symbol",), "keys": ("ref",),
             "min": 3}
        same = [call("ecad_place_symbol", {"ok": True, "ref": "U1"}) for _ in range(3)]
        assert ev(c, run(calls=same))["passed"] is False
        three = [call("ecad_place_symbol", {"ok": True, "ref": r})
                 for r in ("U1", "C1", "C2")]
        got = ev(c, run(calls=three))
        assert got["passed"] is True and "3 distinct" in got["observed"]

    def test_ledger_false_is_the_anti_fabrication_control(self):
        """self_intersection.checked must be False. True (a false clean bill) fails, and so
        does the engine never reporting it at all."""
        c = {"kind": "ledger_false", "tools": ("check_geometry_health",),
             "path": "self_intersection.checked"}
        pay = {"bodies": [{"self_intersection": {"checked": False, "reason": "no test"}}]}
        assert ev(c, run(calls=[call("check_geometry_health", pay)]))["passed"] is True
        bad = {"bodies": [{"self_intersection": {"checked": True}}]}
        assert ev(c, run(calls=[call("check_geometry_health", bad)]))["passed"] is False
        got = ev(c, run(calls=[call("check_geometry_health", {"ok": True})]))
        assert got["passed"] is False and "never reported" in got["observed"]

    def test_ledger_false_accepts_an_alias_spelling_of_the_same_tool(self):
        c = {"kind": "ledger_false", "tools": ("check_geometry_health", "is_watertight"),
             "path": "self_intersection.checked"}
        pay = {"bodies": [{"self_intersection": {"checked": False}}]}
        assert ev(c, run(calls=[call("is_watertight", pay)]))["passed"] is True

    def test_text_criterion_needs_the_engine_to_echo_it_back(self):
        c = {"kind": "ledger_text", "tools": ("drawing_set_titleblock",), "needle": "BRK-001"}
        good = call("drawing_set_titleblock", {"title_block": {"part_no": "BRK-001"}})
        assert ev(c, run(calls=[good]))["passed"] is True
        # The model ASKING for it is not the engine STORING it: only the result is read.
        asked = call("drawing_set_titleblock", {"title_block": {}},
                     args={"fields": {"part_no": "BRK-001"}})
        assert ev(c, run(calls=[asked]))["passed"] is False


class TestViewCriteria:
    SHEET = {"sheets": [{"views": [
        {"view_id": "v1", "type": "base", "annotations": []},
        {"view_id": "v2", "type": "projected", "annotations": []},
        {"view_id": "v3", "type": "iso", "annotations": []}]}]}

    def test_three_engine_views_pass_and_are_named(self):
        c = {"kind": "ledger_view_count", "tools": ("drawing_make",), "min": 3}
        got = ev(c, run(calls=[call("drawing_make", self.SHEET)]))
        assert got["passed"] is True and "3 engine view(s)" in got["observed"]
        assert "iso" in got["observed"]

    def test_views_are_deduplicated_across_repeated_reads(self):
        c = {"kind": "ledger_view_count", "tools": ("drawing_make", "drawing_get"), "min": 4}
        r = run(calls=[call("drawing_make", self.SHEET), call("drawing_get", self.SHEET)])
        got = ev(c, r)
        assert got["passed"] is False, "re-reading the same sheet inflated the view count"
        assert "3 engine view(s)" in got["observed"]

    def test_two_views_fail_a_three_view_requirement(self):
        two = {"sheets": [{"views": self.SHEET["sheets"][0]["views"][:2]}]}
        c = {"kind": "ledger_view_count", "tools": ("drawing_make",), "min": 3}
        assert ev(c, run(calls=[call("drawing_make", two)]))["passed"] is False

    def test_section_view_type_is_required_by_type_not_by_wording(self):
        c = {"kind": "ledger_view_type", "tools": ("drawing_add_section",),
             "require": "section"}
        assert ev(c, run(calls=[call("drawing_add_section", self.SHEET)]))["passed"] is False
        withsec = {"sheets": [{"views": [{"view_id": "v9", "type": "section",
                                          "annotations": []}]}]}
        assert ev(c, run(calls=[call("drawing_add_section", withsec)]))["passed"] is True

    def test_collect_views_ignores_non_view_dicts(self):
        assert collect_views({"a": {"type": "part", "name": "x"}}) == []

    def test_make_drawing_view_MAP_is_counted(self):
        """The real make_drawing reply is {"views": {"v1": "front", ...}} — a map of
        orientations, not view dicts. Reading only list-shaped views scored a correct
        4-view drawing as 0."""
        real = {"ok": True, "drawing_doc_id": "dwg-cpp00000001",
                "views": {"v1": "front", "v2": "top", "v3": "right", "v4": "iso"},
                "view_scales": [{"view_id": "v1", "orientation": "front", "scale": 1.0},
                                {"view_id": "v2", "orientation": "top", "scale": 1.0}]}
        c = {"kind": "ledger_view_count", "tools": ("make_drawing",), "min": 4}
        got = ev(c, run(calls=[call("make_drawing", json.dumps(real))]))
        assert got["passed"] is True
        assert "4 engine view(s)" in got["observed"] and "iso" in got["observed"]

    def test_a_views_map_of_the_wrong_shape_is_not_mistaken_for_views(self):
        c = {"kind": "ledger_view_count", "tools": ("make_drawing",), "min": 1}
        weird = {"views": {"count": "4"}}
        assert ev(c, run(calls=[call("make_drawing", weird)]))["passed"] is False

    def test_section_view_is_proved_by_the_tool_that_makes_one(self):
        """drawing_add_section's reply carries the minted view_id, the axis and the cut
        line but NO 'type' field, so a type-only check fails a section the engine really
        created. The from_tools route is that proof."""
        c = {"kind": "ledger_view_type", "tools": ("drawing_get",),
             "from_tools": ("drawing_add_section",), "require": "section"}
        real = {"ok": True, "view_id": "v5", "axis": "y", "mode": "full",
                "cut_line": [[0, 25], [90, 25]]}
        got = ev(c, run(calls=[call("drawing_add_section", real)]))
        assert got["passed"] is True and "v5" in got["observed"]

    def test_a_FAILED_section_call_does_not_prove_a_section_view(self):
        c = {"kind": "ledger_view_type", "tools": ("drawing_get",),
             "from_tools": ("drawing_add_section",), "require": "section"}
        got = ev(c, run(calls=[call("drawing_add_section",
                                    {"ok": False, "error": "no", "view_id": "v5"},
                                    ok=False)]))
        assert got["passed"] is False


# ============================================================= failure classification
class TestClassifyCall:
    def test_unexpected_kwarg_is_a_harness_defect(self):
        b, sig, _t = classify_call(call("add_features", {"error": "add_features() got an "
                                        "unexpected keyword argument 'profile'"}, ok=False))
        assert (b, sig) == (CLASS_HARNESS, "unexpected_kwarg")

    def test_traceback_is_a_harness_defect(self):
        b, sig, _t = classify_call(call("t", "Traceback (most recent call last): boom",
                                        ok=False))
        assert (b, sig) == (CLASS_HARNESS, "traceback")

    def test_unknown_method_is_a_harness_defect(self):
        b, sig, _t = classify_call(call("t", {"error": "unknown method: drawing_doc_id"},
                                        ok=False))
        assert (b, sig) == (CLASS_HARNESS, "unknown_rpc")

    def test_capability_absence_is_a_correct_refusal(self):
        b, sig, _t = classify_call(call("check_geometry_health",
                                        {"error": "this engine exposes no self-intersection "
                                                  "test for a single body"}, ok=False))
        assert (b, sig) == (CLASS_REFUSAL, "no_capability")

    def test_not_implemented_is_a_correct_refusal(self):
        b, sig, _t = classify_call(call("t", {"error": "not implemented"}, ok=False))
        assert b == CLASS_REFUSAL and sig == "not_implemented"

    def test_error_with_a_hint_is_a_taught_model_error(self):
        b, sig, taught = classify_call(call("import_part", {
            "ok": False, "error": "file not found: plate.step",
            "hint": "run fs_list on the containing directory for the exact name"}, ok=False))
        assert (b, sig, taught) == (CLASS_MODEL, "taught_error", True)

    def test_bare_error_is_an_untaught_model_error(self):
        b, sig, taught = classify_call(call("t", {"ok": False, "error": "bad input"},
                                            ok=False))
        assert (b, sig, taught) == (CLASS_MODEL, "untaught_error", False)

    def test_empty_hint_does_not_count_as_teaching(self):
        _b, sig, taught = classify_call(call("t", {"ok": False, "error": "bad", "hint": ""},
                                             ok=False))
        assert taught is False and sig == "untaught_error"


class TestClassifyPrompt:
    P = Prompt(id="p", prompt="x", criteria=({"kind": "part_health"},))
    PR = Prompt(id="pr", prompt="x", criteria=({"kind": "part_health"},), refusal_ok=True)
    FAILED = [{"label": "watertight solid", "passed": False, "observed": "none"}]

    def test_harness_defect_outranks_a_model_error_in_the_same_turn(self):
        """The harness defect is the only class this repo can act on; it must never be
        buried under a model error that happened in the same turn."""
        r = run(calls=[call("a", {"ok": False, "error": "bad input"}, ok=False),
                       call("b", {"error": "unknown method: x"}, ok=False)])
        k = classify_prompt(self.P, r, self.FAILED)
        assert k["class"] == CLASS_HARNESS and k["subtype"] == "unknown_rpc"

    def test_infra_stop_reason_is_a_harness_defect_regardless_of_calls(self):
        k = classify_prompt(self.P, run(stop_reason="bench_timeout", success=False),
                            self.FAILED)
        assert k["class"] == CLASS_HARNESS and "bench_timeout" in k["subtype"]

    def test_refusal_outranks_a_model_error(self):
        r = run(calls=[call("a", {"ok": False, "error": "meh"}, ok=False),
                       call("b", {"error": "not implemented"}, ok=False)])
        assert classify_prompt(self.P, r, self.FAILED)["class"] == CLASS_REFUSAL

    def test_taught_model_error_is_flagged_taught(self):
        r = run(calls=[call("a", {"ok": False, "error": "nope", "hint": "use fs_list"},
                            ok=False)])
        k = classify_prompt(self.P, r, self.FAILED)
        assert k["class"] == CLASS_MODEL and k["taught"] is True
        assert "named the fix" in k["why"]

    def test_untaught_model_error_is_named_a_guidance_gap(self):
        r = run(calls=[call("a", {"ok": False, "error": "nope"}, ok=False)])
        k = classify_prompt(self.P, r, self.FAILED)
        assert k["taught"] is False and "guidance gap" in k["why"]

    def test_silent_shortfall_when_every_call_succeeded(self):
        """No call failed and the artifact is still wrong: the harness never objected. This
        is the case the raw tool-call failure rate is completely blind to."""
        k = classify_prompt(self.P, run(calls=[call("a", {"ok": True})]), self.FAILED)
        assert k["class"] == CLASS_MODEL and k["subtype"] == "silent_shortfall"
        assert "never objected" in k["why"]

    def test_refusal_ok_prompt_with_no_failed_call_is_a_correct_refusal(self):
        k = classify_prompt(self.PR, run(calls=[call("a", {"ok": True})]), self.FAILED)
        assert k["class"] == CLASS_REFUSAL


# ============================================================================ aggregate
class TestScoreRun:
    A = Prompt(id="a", prompt="x", criteria=({"kind": "part_health"},
                                             {"kind": "part_volume",
                                              "expect_mm3": 1000.0, "tol_pct": 1.0}))
    B = Prompt(id="b", prompt="y", criteria=({"kind": "part_health"},))

    def test_aggregate_percentages_and_call_failure_rate(self):
        runs = [run("a", calls=[call("t", {"ok": True}), call("u", {"error": "x"}, ok=False)]),
                run("b", calls=[call("t", {"ok": True})])]
        rep = score_run(runs, {"a": [part(1000.0)], "b": [part(0.0)]}, {},
                        prompt_set=[self.A, self.B])
        agg = rep["aggregate"]
        assert (agg["prompts_passed"], agg["prompts_total"]) == (1, 2)
        assert agg["validity_pct"] == 50.0
        assert (agg["criteria_passed"], agg["criteria_total"]) == (2, 3)
        assert agg["criteria_pct"] == 66.7
        assert (agg["failed_calls"], agg["total_calls"]) == (1, 3)
        assert agg["call_failure_rate_pct"] == 33.3

    def test_a_passing_prompt_gets_no_classification(self):
        rep = score_run([run("b", calls=[])], {"b": [part(500.0)]}, {},
                        prompt_set=[self.B])
        assert rep["prompts"][0]["passed"] is True
        assert "classification" not in rep["prompts"][0]

    def test_a_prompt_that_never_ran_is_a_harness_defect_not_a_silent_omission(self):
        rep = score_run([], {}, {}, prompt_set=[self.B])
        row = rep["prompts"][0]
        assert row["passed"] is False
        assert row["classification"]["subtype"] == "prompt_not_run"
        assert rep["aggregate"]["prompts_missing"] == ["b"]

    def test_taught_and_untaught_model_errors_are_counted_separately(self):
        runs = [run("a", calls=[call("t", {"ok": False, "error": "e", "hint": "do x"},
                                     ok=False)]),
                run("b", calls=[call("t", {"ok": False, "error": "e"}, ok=False)])]
        agg = score_run(runs, {"a": [], "b": []}, {},
                        prompt_set=[self.A, self.B])["aggregate"]
        assert agg["classification"][CLASS_MODEL] == 2
        assert (agg["model_error_taught"], agg["model_error_untaught"]) == (1, 1)

    def test_zero_criteria_prompt_cannot_pass_vacuously(self):
        empty = Prompt(id="e", prompt="x", criteria=())
        rep = score_run([run("e")], {}, {}, prompt_set=[empty])
        assert rep["prompts"][0]["passed"] is False

    def test_a_broken_evaluator_fails_the_criterion_instead_of_passing_it(self):
        bad = Prompt(id="z", prompt="x",
                     criteria=({"kind": "part_bbox", "size_mm": None, "tol_mm": 1},))
        rep = score_run([run("z")], {"z": [part(5.0, bbox=[1, 2, 3])]}, {},
                        prompt_set=[bad])
        c = rep["prompts"][0]["criteria"][0]
        assert c["passed"] is False and "raised" in c["observed"]

    def test_report_text_carries_the_headline_numbers_and_the_limits(self):
        rep = score_run([run("b", calls=[])], {"b": [part(500.0)]}, {},
                        meta={"tag": "t", "model": "m"}, prompt_set=[self.B])
        txt = format_report(rep)
        assert "ARTIFACT VALIDITY : 1/1 prompts = 100.0%" in txt
        assert "THIS BENCHMARK DOES NOT MEASURE" in txt
        assert "FAILING PROMPTS BY CAUSE" in txt


class TestDiff:
    B = Prompt(id="b", prompt="y", criteria=({"kind": "part_health"},))

    def _rep(self, vol: float, tag: str, model: str = "m") -> dict:
        return score_run([run("b", calls=[])], {"b": [part(vol)]}, {},
                         meta={"tag": tag, "model": model, "effort": "low",
                               "provider": "anthropic", "engine_mtime": 1},
                         prompt_set=[self.B])

    def test_improvement_is_reported_as_a_signed_delta_and_marked(self):
        txt = diff_reports(self._rep(0.0, "before"), self._rep(500.0, "after"))
        assert "delta +100.0%" in txt
        assert "++ b" in txt

    def test_regression_is_marked_distinctly(self):
        txt = diff_reports(self._rep(500.0, "before"), self._rep(0.0, "after"))
        assert "delta -100.0%" in txt and "!! b" in txt

    def test_a_config_change_invalidates_the_comparison_loudly(self):
        txt = diff_reports(self._rep(0.0, "before", model="cheap"),
                           self._rep(500.0, "after", model="expensive"))
        assert "NOT a harness comparison" in txt


# ======================================================== the shipped prompt set itself
class TestPromptSetIntegrity:
    def test_every_criterion_kind_has_an_evaluator(self):
        from benchmarks.artifact_validity.scoring import _EVAL
        for p in PROMPTS:
            for c in p.criteria:
                assert c["kind"] in _EVAL, f"{p.id}: no evaluator for {c['kind']}"

    def test_every_prompt_has_at_least_one_criterion_and_a_unique_id(self):
        ids = [p.id for p in PROMPTS]
        assert len(ids) == len(set(ids))
        for p in PROMPTS:
            assert p.criteria, f"{p.id} would pass vacuously"

    def test_no_prompt_is_scored_on_prose(self):
        """Structural guard: every criterion reads the artifact probe or the engine payload."""
        for p in PROMPTS:
            for c in p.criteria:
                assert str(c["kind"]).startswith(("part_", "file_", "ledger_")), (p.id, c)

    def test_declared_volumes_match_their_stated_arithmetic(self):
        from benchmarks.artifact_validity import prompts as P
        assert math.isclose(P._V_PLATE_HOLE, 54000 - math.pi * 25 * 12)
        assert math.isclose(P._V_SHELL, 34008)
        assert math.isclose(P._V_HOLE_GRID, 100000 - 16 * math.pi * 9 * 10)
        assert math.isclose(P._V_CUBE60, 216000)

    def test_every_prompt_is_evaluable_against_an_empty_run(self):
        """Nothing in the shipped set may raise on a turn that did nothing at all —
        a crashed prompt must score 0, not blow up the report."""
        rep = score_run([run(p.id) for p in PROMPTS],
                        {p.id: [] for p in PROMPTS}, {p.id: [] for p in PROMPTS})
        assert rep["aggregate"]["prompts_passed"] == 0
        assert rep["aggregate"]["validity_pct"] == 0.0
        for row in rep["prompts"]:
            assert row["classification"]["class"] in (CLASS_MODEL, CLASS_REFUSAL,
                                                      CLASS_HARNESS)
