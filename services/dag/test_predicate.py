"""Allowlisted predicate semantics and the audited abs.__self__ escape."""
from __future__ import annotations

import pytest

from services.dag.verify import _eval_predicate, run as verify_run, VERDICT_ESCALATE


@pytest.mark.parametrize("expression", [
    "not result.get('interferences')",
    "result.get('errors', 1) == 0",
    "result.get('unit_cost', 9e9) <= target_budget",
    "len(result.get('items', [])) == 3 and result['items'][1] == 2",
    "result['nested'].get('safe') and result['items'][-1] == 3",
    "result['items'][0:2] == [1, 2]",
    "abs(-3) + 4 * 2 / 2 == 7",
    "(9 // 2) == 4 and 9 % 2 == 1 and 2 ** 3 == 8",
    "all([True, 1]) and any([False, True])",
    "min(result['items']) == 1 and max(1, 2, 3) == 3",
    "int('2') == 2 and float('2.5') == 2.5 and bool([]) == False",
    "0 < result['items'][1] <= 3 and 'safe' in result['nested']",
    "{'a': (1, 2)}['a'][0] == 1 and None is None",
    "False or True",
])
def test_benign_predicates_preserved(expression):
    result = {"interferences": [], "errors": 0, "unit_cost": 50,
              "items": [1, 2, 3], "nested": {"safe": True}}
    assert _eval_predicate(expression, result, {"target_budget": 100}) == (True, None)


@pytest.mark.parametrize("expression", [
    "abs.__self__.open('/unused').read() == ''",
    "abs.__self__.__import__('os').system('unused')",
    "result.__class__.__mro__[1].__subclasses__()",
    "result.get('__class__').__mro__",
    "(lambda: True)()",
    "[x for x in result]",
    "all(x for x in result)",
    "{x: x for x in result}",
    "{x for x in result}",
    "__import__('os')",
    "open('/unused')",
    "result['callable']()",
    "getattr(abs, '__self__')",
    "result.clear()",
    "result.get.__call__('safe')",
    "True or abs.__self__.open('/unused')",
    "False and (lambda: True)()",
    "2 ** 10000000",
    "(2 ** 100) ** 100",
    "'x' * 100000000",
    "[0] * 100000000",
    "float('inf') == float('inf')",
    "1e999 == 1e999",
    "(a := 1) == 1",
])
def test_escape_and_resource_payloads_refused(expression):
    passed, error = _eval_predicate(expression, {})
    assert passed is False
    assert error and error.startswith("predicate evaluation error:")


def test_audited_file_escape_never_opens_marker(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("fixture-only")
    expression = f"abs.__self__.open({str(marker)!r}).read() == 'fixture-only'"
    passed, error = _eval_predicate(expression, {})
    assert passed is False and error
    assert marker.read_text() == "fixture-only"


def test_arbitrary_data_objects_never_invoke_magic_methods():
    class Hostile:
        def __bool__(self):
            pytest.fail("untrusted truthiness was invoked")
        def __eq__(self, other):
            pytest.fail("untrusted comparison was invoked")
    assert _eval_predicate("result['value'] == 1", {"value": Hostile()})[0] is False
    assert _eval_predicate("bool(result['value'])", {"value": Hostile()})[0] is False


def test_escape_escalates_actual_verify_dispatch():
    node = {"id": "verify", "type": "verify", "name": "Fixture", "autonomy": "auto",
            "on_fail": "pause", "inputs": {"check_type": "interference"},
            "end_condition": "abs.__self__.__import__('os')"}
    verdict, output = verify_run(node, {"node_id": "verify", "node_inputs": node["inputs"],
                                      "retry_remaining": 1, "end_condition": node["end_condition"]},
                                 tool_fn_override=lambda **_: {"ok": True, "interferences": []})
    assert verdict == VERDICT_ESCALATE
    assert output["data"]["reason"] == "predicate_error"
