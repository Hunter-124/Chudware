---
name: chudware-harness-integrity-audit
description: Audit agent harness dispatch, guidance, guardrails, and test isolation.
---

# chudware-harness-integrity-audit

- Begin with a targeted reproduction and affected tests. Trace online advertised tools through `omp_tools.build_host_tools`, extension dispatch and delivered session context; the Python `agent_turn` binder is an offline path, not proof of online reachability.
- Check advertised-but-unbound tools, unsatisfiable guardrails, budget starvation and fabricated success against real dispatch. Confirm a same-batch reference was not incorrectly rejected.
- When using a corpus, treat run-ID filters as hypotheses rather than proof of genuine user runs; do not infer current failure rates from historical counts. Replay only with a private hostd.
- Compare with a dependency-equivalent isolated baseline, never stash shared work. Check clean-config behavior, import/collection-order pollution, private daemon state and false-green tests; collection-time pollution can originate from later modules.
- Use positive controls for screenshots, glyph rendering and stub leakage when relevant. Report actual failures.

Verify: `scripts/testbox.py --suite agent`
