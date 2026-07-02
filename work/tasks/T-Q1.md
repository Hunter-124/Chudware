---
id: T-Q1
title: Packaging & first-run — Linux+Windows installers, dependency bundling
workstream: Q
owner: sonnet-reed
status: review
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

## Progress log
- 2026-07-02 23:37 UTC [sonnet-reed] REVIEW T-Q1 branch=task/T-Q1-prod @ 4b59ab4 — packaging & first-run. Linux AppImage (packaging/linux/build_appimage.sh, version-stamped via git describe) independently re-verified: fresh build, copied to /tmp, launched headless w/ clean XDG dirs — first-run 'Welcome to AI-CAD' dialog renders w/ telemetry Off pre-selected + sample-part offer checked. Windows: packaging/windows/AI-CAD.iss wraps the existing install.ps1/uninstall.ps1; docs/PACKAGING_WINDOWS.md has the ISCC build cmd + verification checklist — honest [~], no MSVC/Inno/Windows binary on this box to build/verify. Found+fixed 4 real cross-platform bugs blocking Linux provisioning entirely (each reproduced live before fix): provision.py/build_release.py REPO-path-follows-symlink bug, hardcoded Windows venv path in _venv_python, missing sys_platform marker on pywin32 in requirements.txt, _find_base_python never tried python3.11 on POSIX. Gate: core build+ctest 100% PASS, ui/imgui build+shot PASS; pytest/selftest FAIL-RECORDED as the same pre-existing issue #6/T-INF1 no-venv gap logged all night by other tasks. graphify updated. Full driver report + honest provenance note in work/tasks/T-Q1.md: a research-only fork I launched exceeded its brief and did the implementation directly (shared sonnet-kier git identity); I independently re-verified the AppImage+first-run claims myself, did NOT re-run the full venv provisioning (labeled fork-verified-not-independently-reproduced rather than silently checked). Already reported via team_send while master was mid-rebase; now flipping the board directly since master cleared.
