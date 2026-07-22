---
id: T-RF12
title: RF wave-2 web degradation UX — CEF panels + explicit web_unsupported gating (node R2c)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF12-web-degradation
depends_on: [T-RF3, T-RF6]
lane: app-gui
kind: fix
priority: 3
---

## Context

On the WASM build, CEF is forced OFF so vendor sign-in / web panels (web_panel.cpp,
oauth_capture.cpp, parts_browser_panel.cpp) degrade to NullWebView — silent dead
buttons. Python-worker-only methods are unreachable from web until the zero-Python
stream (T-RF15+) lands. contracts/web_parity.md (T-RF3) inventories the gaps; the UI
should render them as gated, not dead.

## Scope

Audit CEF-dependent + python-worker-dependent ribbon/PM entries on the WASM build; gate
them behind a capability flag with a clear "desktop only" affordance. Wire the flag to
the contracts/web_parity.md inventory so the list self-documents and shrinks to zero at
T-RF21. WASM-shot verify the gated states.

## Definition of done

- [ ] No silent dead buttons on the web build for inventoried gaps.
- [ ] Capability flag reads the web_parity inventory (single source).
- [ ] WASM + native shots; gate.sh green; REFACTOR_LOG handoff.
