---
name: chudware-web-gateway-deploy
description: Deploy and verify the Chudware web gateway using the documented single-operator configuration.
---

# Web gateway deployment

Use `web/README.md` as the authority for prerequisites, Compose deployment, remote HTTPS authentication, persistence, and isolation. Gateway code lives in `web/gateway/`; the separate public site is in `website/`.

1. Choose any remote target from the operator's own SSH configuration. Do not embed hostnames, addresses, usernames, credentials, or local token paths in project documentation or logs.
2. Follow the README's current deployment and security instructions. The gateway serves one trusted operator; proxy authentication does not create isolated CAD accounts. Do not expose it to mutually untrusted users or weaken the documented sandbox to work around a failed isolation probe.
3. Verify changes in a separate isolated deployment with private workspace and state, following the README's verification guidance. Do not restart, interrupt, or mutate an open user session or shared engine for verification.
4. Preserve the selected image and persistent data in accordance with the README's backup and rollback guidance. Avoid destructive cleanup or unrelated site changes.

Verify: `scripts/testbox.py --suite platform`
