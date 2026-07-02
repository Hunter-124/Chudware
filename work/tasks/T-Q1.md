---
id: T-Q1
title: Packaging & first-run — Linux+Windows installers, dependency bundling
workstream: Q
owner: unassigned
status: todo
branch: task/T-Q1-prod
day: 1
priority: 2
depends_on: []
---

## Context

PROJECT-LEAD ADDITION: no packaging story exists. ShopCraft must install and first-run
cleanly on a fresh machine (both OSes) with the Python worker + mcp-venv + assets
bundled — today only developers can run it.

## Files / areas touched

- packaging/: Linux AppImage (primary) + Windows Inno/MSI script (build-side; document
  the Windows-host steps you cannot run here), version stamping from git tag
- First-run: detect missing runtime deps, create mcp-venv, offline-friendly; first-run
  panel (uses T-U1 patterns if merged, plain otherwise): telemetry consent prompt
  (default meta? NO — default OFF per ratified external-user rule), sample project offer

## Definition of done

- [ ] AppImage builds in CI-able script form and launches on this box from /tmp
- [ ] Windows packaging script + doc complete enough for a Windows host to execute [~]
- [ ] First-run flow: consent prompt (default OFF), venv bootstrap, opens sample project
- [ ] gate; graphify update .

## Driver notes (Fable)

- Bundle policy: permissive-licensed only in-image; GPL subprocess tools (pandoc etc.)
  are DISCOVERED, never bundled — the licenses.md ledger is the authority. PyMuPDF never.
- linux-build-port memory/docs list the .so + venv landmines; read docs/ for the staged
  services/dist layout (that IS the proto-package — formalize it, don't reinvent).
