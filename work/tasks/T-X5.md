---
id: T-X5
title: Profile manager UX seam - list/refresh/delete site profiles + expiry detection
workstream: X
owner: gissa
status: doing
branch: task/T-X5-profile-manager-seam
depends_on: [T-X1]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Users will accumulate browser session profiles across sites (GrabCAD,
TraceParts, McMaster, SnapEDA, manufacturer portals...) over time. This task
gives them (and the copilot) visibility and control: which sites have a saved
profile, when it was last used/logged in, and a way to refresh (re-login) or
delete a profile. It also detects login *expiry* (a saved profile that no
longer authenticates) so the copilot can proactively prompt a re-login instead
of an adapter silently failing partway through a fetch.

This task defines the tool surface and the JSON shape a future GUI panel will
consume — the GUI panel itself is explicitly OUT of scope here (per the
mission brief, "GUI panel is a LATER task").

## Files / areas touched

- `services/mcp/parts_tools.py` (or a small new `services/mcp/browser_tools.py`
  if `parts_tools.py` is getting crowded — prefer the smallest file split that
  keeps the "one seam" instruction from the mission brief true; check file size
  before deciding) — new MCP tool `browser_profiles(action, site?)`:
  - `action="list"` → `{ok, profiles: [{site, created_ts, last_used_ts,
    last_login_ts, status}]}` where `status` is `"active" | "expired" |
    "unknown"`.
  - `action="refresh"` → re-invokes `browser_login(site)` (headed, user
    completes login again).
  - `action="delete"` → removes the profile dir + metadata sidecar for `site`.
- `services/browser/profiles.py` (T-X1) — extend with an `expiry check`
  primitive: a lightweight per-site "am I still logged in" probe (e.g. fetch a
  known post-login page via `browser_fetch` and check for the login-wall
  signal T-X2's adapters already detect — reuse that detection logic, don't
  duplicate it) that marks a profile `expired` in its metadata without
  requiring a full adapter fetch.
- `services/browser/test_profile_manager.py` — list/refresh/delete against
  faked profiles; expiry detection against a faked login-wall response.

## Steps

1. `graphify query "MCP tool register FastMCP parts_tools"` to confirm the
   registration pattern (`register_parts_tools(mcp)` style) so the new tool
   follows the same convention.
2. Define and freeze the `browser_profiles` JSON shape (this is what the later
   GUI task will consume — get the field names right the first time; mirror
   the field names already used in T-X1's metadata sidecar so there's no
   translation layer).
3. Implement list/refresh/delete.
4. Implement the expiry probe, reusing T-X2's login-wall detection (extract it
   to a shared helper if it's currently adapter-private).
5. Tests.

## Definition of done

- [ ] `browser_profiles("list")` returns every saved profile with an accurate
      `status`
- [ ] `browser_profiles("refresh", site)` re-opens a headed login window for
      that site
- [ ] `browser_profiles("delete", site)` removes the profile completely (dir +
      metadata, no orphaned files)
- [ ] Expiry detection reuses (not duplicates) T-X2's login-wall signal
- [ ] JSON shape documented in the tool's docstring clearly enough that a
      later GUI task can consume it without re-deriving it from code

## AI hints

- Keep this additive to T-X1 — you're building the control surface over
  profiles that already exist, not changing how profiles are created/stored.
- Don't build the GUI. Don't be tempted to. Define the JSON, stop there.

## Progress log
