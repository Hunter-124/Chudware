---
name: chudware-general-lane-triage
description: Trace agent run failures through online OMP sessions.
---

# chudware-general-lane-triage

- Start with the literal error and search its producer, surrounding context, tool inputs and events. Check whether a reported missing reference was created earlier in the same operation batch before changing guardrails.
- For runs trace GUI → `agent_admin_turn` → `OmpSessionHost` → OMP host-tool binding, dispatch, mapped events and settlement. Do not assume the older Python turn loop is the live runtime.
- For truncated replies, inspect the provider stop reason when available, then textual indicators such as unmatched directive fences and unbalanced braces outside quoted strings. Consider output caps and reasoning budgets. Trace settings from persistence to their actual runtime consumers and advertised/enforced tools.
- Replay the failing reply and complete/prose negative controls against the actual isolated OMP runtime path, never a live admin turn.
- Compare delivered guidance with actual tool bindings, policy, budgets and completion evidence. Preserve session ownership, cached context and thought traces. Historical traces and provider costs are observations, not a current baseline.
- Compare against a dependency-complete isolated baseline without stashing shared work; use a private daemon.

Verify: `scripts/testbox.py --suite agent`
