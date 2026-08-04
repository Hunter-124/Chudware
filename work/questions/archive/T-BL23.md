# T-BL23 questions (sonnet-gorse)
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 — req_join met-statuses: keep legacy aliases or drop to ladder-only?

**Context.** DoD item 5 says "reconcile req_join's `met_statuses = ("pass","verified","ok")`
onto the T-H3 `VALID_STATUSES` ladder."  Two interpretations:

**Option (a) — implemented, non-breaking.**  Keep `"pass"` and `"ok"` as explicit legacy
aliases alongside the canonical `_VERIFIED_STATUS` (derived from the ladder).  `_VERIFIED_STATUS`
now propagates if the schema ever renames `"verified"`.  All existing tests hold.
This is the safe default and is what the branch ships.

**Option (b) — drop aliases, ladder-only.**  Replace `_REQ_MET_STATUSES` with just
`frozenset(VALID_STATUSES) - {"proposed", "user_confirmed", "failed", "waived"}` (i.e.,
`{"verified"}`).  This makes `"pass"` and `"ok"` fail the join, which would break
`test_req_join_all_pass` and any requirement documents that record `status: "pass"`.
Hard rule says don't weaken existing assertions — so this path requires an explicit
advisor ruling that the test expectation itself should change.

**Recommended answer:** option (a) is already shipped and sufficient for the DoD.
Option (b) is a future cleanup only if the team decides to ban legacy aliases everywhere
(a contracts-layer decision, not a battery fix).  No action needed unless advisor disagrees.

## ANSWER (Fable foreman, 2026-07-03)

Q1 (drop pass/ok legacy aliases, ladder-only?): **Option (a) stands — keep the aliases.** Documented, tested, and dropping them breaks test_req_join_all_pass for zero benefit today. Revisit only if a req-schema version bump forces migration anyway.

Chips: `_gap_eval_check_direct` scalar-only bug was fixed by Hunter directly on master (5336cd1) while you were in flight — your branch's tests were union-merged with his (38/38 green). contracts.md drift cut as T-BL32.
