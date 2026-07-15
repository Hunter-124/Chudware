# mega-24 a2 offline proof (Fixer24 = ComingOcelot)

**Date:** 2026-07-15  
**No xai / no openrouter spend.** Local deterministic provider only.

## What a2 failed on (live)

Archive: `attempt-thrash-20260715T223717Z`

| Node | Assign | Failure |
|---|---|---|
| n_spec_DL3 | xai-sub/grok-4.5 | no_code (0 tools, ~29 tokens) |
| n_spec_DL4 | xai-sub/grok-4.5 | no_code (0 tools, ~29 tokens) |
| n_layout | xai-sub/grok-4.5 | no_code (0 tools, ~18 tokens) |
| n_plan | xai-sub/grok-4.5 | delegate_or_typed_refusal_required after tools |

## Rebind under test

1. `_force_first_tool` — zero-tool no_code forces `ledger_get` once, continues loop  
2. `_attach_typed_refusal_if_needed` — formal/bare done after tools gets typed_refusal  
3. Free-form mega-8 contract still blocks (ledger_note+buildgraph_plan without envelope)

## Offline scenarios (all green)

```
run_turn layout (empty first reply) → force ledger_get → second completion done
  success=True stop=confirmed forced=True typed_refusal present

n_plan gate with tools + typed_refusal → VERDICT_DONE (not delegate_or_*)

n_plan free-form (note only) → still retry/delegate_or_typed_refusal_required

spec_dl3 empty first reply → force + done confirmed
```

Command:

```bash
python3 - <<'PY'
# (see ComingOcelot session; result OFFLINE_PROOF_OK)
PY
```

## Gate

`230 passed` (mega11+mega16..24+mega8+mega10+autosave) including unit tests for
force-first-tool and typed_refusal attach.

## Ready for a3?

**Yes from offline proof.** Live still depends on grok not hanging forever after
the forced tool result; the no_code thrash class from empty first replies is
closed offline.

No push. S23/T23 preserved.
