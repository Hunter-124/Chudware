---
name: chudware-xai-oauth-recovery
description: Diagnose xAI subscription refresh failures and usage polling without disturbing active credentials.
---

# xAI subscription recovery

For `invalid_grant` or revoked-refresh errors, inspect the refresh and credential-merge paths in `services/agent/providers/xai_sub_provider.py`, `sub_auth.py`, and `subscription_login.py`, and usage polling in `usage.py`. Do not print, copy, or mutate real credentials during diagnosis.

Refresh tokens can rotate on exchange; concurrent refreshes or stale writes can lose newer tokens. Preserve token-safe credential merges and distinguish revoked refresh credentials from a still-valid access token. The usage path skips the relevant live fetch for credentials that are both revoked and expired, not merely one or the other. When investigating multiple accounts, check candidate selection and polling behavior rather than assuming the first credential is usable.

Use isolated mocked credentials and provider responses for regression cases, including refresh rotation and revoked/expired combinations. Apply code changes in a separately launched instance or on the next natural restart; never restart an open user session. If reauthentication is necessary, have the user sign in through the application's xAI settings; never invent or extract tokens.

Verify: `scripts/testbox.py --suite agent`
