---
name: chudware-recovery-tier-doc-probe
description: Diagnose unexpected recovery tabs and internal-document orphan handling.
---

# chudware-recovery-tier-doc-probe

Distinguish ordinary visible documents from recovery-only artifacts. Historical `_pf` debris may originate in older preflight shadow-clone implementations; current `services/agent/advisor/preflight.py` does not create registered clones or replay history. Do not infer current behavior from a suffix alone.

Trace internal document handling across `core/hostd/doc_registry.cpp`, autosave, orphan grace and recovery listing. Reproduce using private data roots and the `chudware-isolated-hostd-probe` recipe. Verify intended user documents remain recoverable while internal artifacts do not leak; use isolated fixtures or a separate baseline worktree, never stash shared work.

Verify: `scripts/testbox.py --suite platform`
