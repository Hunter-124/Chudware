---
name: chudware-agent-domain-coverage
description: Extend agent domain coverage across roles, skills, guidance and tool families.
---

# chudware-agent-domain-coverage

- Distinguish roles (`services/agent/roles/`), builtin skills, advisory triggers and context packs, prompt cheatsheets, tool families/options, and recipes; each solves a different reachability problem.
- Add only the surfaces required by the domain. Keep trigger definitions and packs synchronized, update the affected current contract, and update registration and tests when family metadata changes. `services/mcp/server.py` also performs post-registration family retagging.
- Check policy defaults rather than assuming every family is enabled: manufacture defaults off. Review tool options and the actual online OMP bindings for reachability.

Verify: `scripts/testbox.py --suite agent`
