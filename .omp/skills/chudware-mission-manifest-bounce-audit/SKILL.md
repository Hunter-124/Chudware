---
name: chudware-mission-manifest-bounce-audit
description: Diagnose mission manifest validation bounces and distinguish mechanical omissions from missing intent.
---

# chudware-mission-manifest-bounce-audit

Capture the complete recorded `mission_start` arguments and validator errors from supplied evidence; screenshots can truncate them. Inspect normalization and drafting in `services/agent/pipeline/manifest.py`. Distinguish mechanical information already available to the harness from semantic choices only the requester or model can make. Normalize only unambiguous mechanical omissions, and preserve actionable validation for missing intent.

Inspect trigger mirrors and their parity coverage in `services/agent/advisor/test_triggers.py`. Reproduce offline with private fixtures, exercise both repaired and genuinely invalid manifests.

Verify: `scripts/testbox.py --suite agent`
