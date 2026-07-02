# align-post-merge — observations and constraint notes (sonnet-wick)

## Item 1: Groundedness conformance adapter — DEFAULT FLIP BLOCKED BY EXISTING TEST

**What was done:** `H4GroundednessAdapter` class created in
`tools/translator/groundedness.py`, wrapping the T-H4 `_verdicts_from_state()` core
and implementing the `GroundednessChecker` Protocol. The adapter is in `__all__` and
installable via `set_checker()`. 14 adapter conformance tests added in
`tools/translator/test_groundedness_adapter.py`. Both existing suites (23 battery +
34 harness) stay green unmodified at 57 passed.

**The default flip constraint:** The task description says "make set_checker()/module
default install it, retiring `_MembershipStub` as default." However:

- `tools/translator/eval/tests/test_slot_eval.py:164` asserts:
  `assert groundedness.is_stub() is True`
- This test is in the "T-F10 harness tests (34 in tools/translator/)" that the task
  says must stay green UNMODIFIED.
- Flipping the module-level default from `_MembershipStub()` to `H4GroundednessAdapter()`
  would cause `is_stub()` to return `False`, breaking that test.

**Recommendation:** Either:
  (a) Update `test_groundedness_is_stub_by_default` to accept `is_stub()` returning
      False when the adapter is installed as default (one-line change to an existing
      test, which relaxes "unmodified" for that one test), OR
  (b) Keep `_MembershipStub` as the process-start default and require callers to
      explicitly `set_checker(H4GroundednessAdapter())` at startup (e.g., in
      `services/mcp/server.py` or wherever the battery is initialized).

The adapter class itself is complete and correct; only the auto-install step waits
for this ruling.

---

## Item 3: T-H3 imports services/verify/unit_ranges.RANGES — PREMISE ABSENT

**Finding:** `engine_oracle/requirements/requirements.py` (607 lines, T-H3's merged
module) contains NO `RANGES` table and NO quantity range/min-max validation. The
`Quantity` dataclass holds `param/op/value/unit/tol` but never validates against
physical plausibility bounds. There is no duplicate of `services/verify/unit_ranges.RANGES`.

**T-H4.md item #3 was a pre-emptive recommendation** to T-H3 to import RANGES rather
than duplicate it. T-H3 simply did not add range validation — so there is nothing to
deduplicate.

**Recommendation:** No code change needed. If quantity range validation is desired in
T-H3's module in the future, it should import RANGES from `services/verify/unit_ranges`
per the T-H4.md recommendation. Filed as a note; skip as a code item.
