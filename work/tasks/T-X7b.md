---
id: T-X7b
title: Vendor wave-2 follow-ups — McMaster STEP import + structured price to bom_price seam
workstream: X
owner: unassigned
status: todo
branch: task/T-X7b-vendor-wave2-followups
depends_on: [T-X7]
lane: parts-sourcing
kind: new
priority: 4
---

## Context

T-X7 (merged) shipped the 4 wave-2 vendor adapters (Amazon/McMaster/JLCPCB/PCBWay) as
part_search federation providers with 45 tests + the vendor survey. Two DoD items were
honestly left partial and split here:

- McMaster STEP -> import_part with source metadata: adapter acquire() returns the
  reference but the STEP fetch -> import_part wiring is incomplete.
- Structured price/availability -> bom_price approved-provider seam: NOT DONE.

## Definition of done
- [ ] McMaster (and where available) STEP acquire -> import_part with source metadata.
- [ ] Structured price/availability feeds the bom_price approved-provider seam.
- [ ] Tests; scripts/gate.sh PASS; graphify update.

## Locked decisions

- **No McMaster API key (Hunter, 2026-08-03).** McMaster will not issue one, so the
  acquisition path is the **CEF driving method**, not a vendor API: the interactive
  sign-in panel (`host/src/ui/widgets/cef/`, `docs/design/cef_webview.md`) establishes the
  session in the shared warm profile, and `services/browser/cef_client.py` drives that same
  profile headlessly. `services/browser/adapters/mcmaster.py::acquire` already resolves the
  client-minted `.step` download URL by running the page's own JS — the remaining work is
  wiring its result into `import_part` with source metadata, NOT adding an API client.
  Any adapter written against a hypothetical McMaster API is out of scope.
- Structured price/availability still feeds the `bom_price` approved-provider seam from
  whatever the CEF fetch parsed; a scraped price must be labelled with its provenance and
  timestamp rather than presented as a vendor-quoted figure.
