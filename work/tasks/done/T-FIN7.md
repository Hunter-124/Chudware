---
id: T-FIN7
title: Emulation power/duty-cycle profile - per-rail activity profile artifact from fw_sim_run
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN7-fw-power-profile
depends_on: [T-FIN3]
lane: ecad-firmware
kind: new
priority: 3
---

## Context

Derive a power-relevant activity profile from emulation: CPU run/sleep residency,
peripheral active windows, GPIO/PWM duty cycles, radio-on windows where modeled — computed
from the T-FIN1 trace + scenario timeline. Combine with per-component current draw metadata
(T-FIN8: active/sleep currents per rail) into a duty-cycle-weighted current profile per
rail (aicad.powerprofile/1 artifact). This is deliberately a bookkeeping model, not a SPICE
power sim — honest labels. Consumed by T-FIN23 (system power budget) and usable as a
duty-cycle heat load for transient thermal (T-FIN18).

## Files / areas touched

- engine_oracle/firmware/powerprofile.py — trace -> residency/duty extraction
- contracts/firmware_rpc.md — aicad.powerprofile/1 schema
- services/mcp/firmware_tools.py — expose profile in fw_sim_run result / artifact
- tests over canned traces

## Definition of done

- fw_sim_run can emit a power profile artifact for a scenario run
- Profile = per-rail average + peak current with residency breakdown, provenance to trace
- Schema frozen; consumed downstream without re-parsing raw traces
