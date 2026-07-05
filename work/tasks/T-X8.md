---
id: T-X8
title: In-app visible browser login - headed session window launched from the app, profile capture, expiry re-login
workstream: X
owner: unassigned
status: todo
branch: task/T-X8-headed-login-window
depends_on: [T-X1, T-X5]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Hunter directive 2026-07-05 (priority #4): "some way to login via a built in browser to
web accounts for popular vendors … non headless browser integration for account logins."
T-X1's session service already persists per-site Playwright profiles; what is missing is
the USER-FACING flow: a visible (headed) browser window the user opens from inside
AI-CAD to log in to a vendor, which then persists that session for the headless
adapters (T-X2/T-X7) to reuse.

## Definition of done

- [ ] `services/browser/headed_login.py`: launch a HEADED Playwright window for a named
      site profile (same profile dir the headless side uses), navigate to the vendor
      login page, and detect login success (site-specific logged-in marker from the
      adapter) — then close and mark the profile fresh. The user types credentials into
      the real vendor page THEMSELVES; we never see, proxy, or store credentials, and we
      NEVER automate past captcha/2FA (they are the user's to solve, in this window).
- [ ] MCP tool (services/mcp) so the in-app copilot can trigger "log in to <vendor>"
      when an adapter reports login_required/expired; returns profile freshness state.
- [ ] GUI seam: sourcing/settings panel lists site profiles (T-X5 seam) with status
      (fresh/expired/absent) and a "Sign in…" button per vendor that fires the headed
      flow; toast on success. Keep the panel minimal — full sourcing UX comes with the
      ordering task (T-X9). Coordinate with T-G23's chrome (new glass-column panels).
- [ ] Works on Linux under a real DISPLAY; when DISPLAY is absent, returns a clear
      "needs a desktop session" error instead of hanging (never xvfb — the whole point
      is the user seeing the window).
- [ ] Tests: profile freshness state machine + login-marker detection mocked; headed
      launch smoke gated behind an env flag (CI-safe skip).

## Driver notes

- graphify rule: `graphify query "<question>"` before raw-file reads (subagents too).
- Branch task/T-X8-headed-login-window, worktree ../CAD-T-X8. Stop at status: review.
- Security posture is contract-level: no credential storage, no captcha/2FA automation,
  profiles local-only and gitignored. Restate this in module docstrings.
- Python venv for gate: `~/.local/share/AI-CAD/mcp-venv/bin/python`.
