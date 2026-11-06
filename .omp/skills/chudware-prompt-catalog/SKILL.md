---
name: chudware-prompt-catalog
description: Trace prompt catalog edits from Settings to mission role prompts and overrides.
---

# chudware-prompt-catalog

`services/agent/roles.py` catalogs mission/YAML roles. Entries use `mission:<id>` and the appdata override `prompts/<id>.mission.md`. `resolve_role_system_prompt` chooses a nonempty mission override, then authored YAML text, then the built-in prompt (`services/agent/prompts/<id>.md`, used for fallback only).

Trace `list_editable_prompts`, save operation and role dispatch before changing UI or runtime. Recheck the current Settings UI rather than assuming a fixed tab inventory. Use private appdata for override tests.

Verify: `scripts/testbox.py --suite agent`
