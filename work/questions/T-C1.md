# T-C1 — questions / escalations

## Q1 (BLOCKER, cross-workstream): `scripts/gate.sh` fails on a pre-existing `ui/imgui` C++ break

**Status:** open — needs a GUI-workstream fix, NOT fixable within T-C1's allowed files.

The gate's `ui/imgui` stage fails to build with:

```
ui/imgui/src/ui/property_manager.cpp:2265:45: error:
  'const struct chudware::ui::PanelField' has no member named 'selMulti'
```

`property_manager.cpp` references `def->selMulti` in **10** places (lines 846, 902, 1072,
1095, 1358, 1373–1379, 2265, 2276…), but `struct PanelField` in
`ui/abstract/panel_spec.h` has **no** `selMulti` member (it has `visibleWhenExpr`, etc.).

**This is pre-existing on master.** Verified at the tip commit *before* the T-C1 claim
(`f6e156c`): `git grep -c selMulti f6e156c -- ui/imgui/src/ui/property_manager.cpp` → 10,
while `panel_spec.h` has 0. It was introduced by the liquid-glass v2 GUI work (commit
`0c20c37` and the follow-on GUI-styling commits). T-C1 adds **only** Python + a contract
`.md` + a firmware CMake template — zero C++ — so it cannot cause or fix this.

**Recommendation:** a GUI-workstream task adds `bool selMulti = true;` (or the intended
default) to `PanelField` in `ui/abstract/panel_spec.h` and populates it in the panel-spec
generator, OR removes the `selMulti` uses in `property_manager.cpp`. Until then the shared
gate is red for every branch, independent of T-C1.

**Impact on T-C1:** all functional DoD items are met and verified (see the task file). The
only failing gate stage is this pre-existing GUI break. Marking `status: review` with this
flagged so the integrator knows the gate red is not from T-C1 and the firmware slice is
merge-ready on its own.

## Q2 (minor, non-blocking): firmware toolchain license row

`contracts/licenses.md` says "adding any dependency requires a row here." The firmware
toolchains (arm-none-eabi-gcc, CMake, ESP-IDF, arduino-cli) are **external subprocess CLIs,
not linked/vendored deps**, so `firmware_rpc.md` §1 documents their subprocess-only posture
inline (the ngspice precedent). I did **not** edit `contracts/licenses.md` since it is
outside T-C1's "Files / areas touched" and contracts are append-only without advisor sign-off.

**Recommendation:** when the day-2 MCP registration lands, add a short "Firmware toolchain
posture" section to `contracts/licenses.md` mirroring the ECAD section. Confirm this is the
desired home for it.
