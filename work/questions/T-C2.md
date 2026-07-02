# T-C2 questions (opus-crag)

## Q1 — Relax `fw_new_project.mcu_family` to optional (contract §5)

**Context.** `contracts/firmware_rpc.md` §5 lists `fw_new_project` params as
`{name, mcu_family, toolchain?, board_doc_ref?}` — `mcu_family` is not marked optional.
But T-C2's DoD requires: *"`new_project()` auto-selects MCU family from ECAD BOM when
`mcu` param not specified."* Auto-selection is only reachable if `mcu_family` may be
omitted. This is a contract signature relaxation, so I am not deciding it unilaterally.

**Recommended answer (implemented on-branch, backward-compatible).**
Make `mcu_family` **optional**:
- If `mcu_family` is given → used verbatim (existing behaviour, unchanged).
- If omitted **and** `board_doc_ref` resolves to a board/schematic doc → derive the
  family from the first MCU component in the BOM (`STM32*`→`cortex-m4`, `ESP32*`→
  `xtensa-lx6`, `ATmega*`→`avr`; lookup in `engine/firmware/mcu_pinouts.py`).
- If omitted **and** no family is derivable → error (`ValueError` at the Python layer /
  error envelope at the MCP layer). Never silently default to a wrong MCU.

This is purely additive (a required param becomes optional); every existing caller that
passes `mcu_family` is unaffected. If the supervisor prefers `mcu_family` stay strictly
required, say so and I'll drop the auto-select path (but the DoD then can't be met).

## Q2 — `fw_build` return shape for tool-absent: BuildResult dict vs error envelope

**Context.** Contract §5 says `fw_build` returns a `toolchain_not_found` **error
envelope** when the backend CLI is absent. But §3 defines `BuildResult` with
`returncode = -1` meaning *"tool not found / timeout"*, and T-C2's DoD says
*"`fw_build` returns `{success, elf_path, stderr, warnings}` dict"*.

**Recommended answer (implemented).** `fw_build` returns the **`BuildResult` dict**
always, per the DoD. The tool-absent case is encoded inside it: `success=False`,
`returncode=-1`, `stderr` prefixed `toolchain_not_found: …`. This is losslessly
detectable by callers (`returncode == -1`) and matches the DoD's stated shape and the
scaffold's non-raising `Toolchain.build()`. If the supervisor wants the strict §5 error
envelope for absent tools instead, it's a one-line branch in `mcp_tools.fw_build`.

_Both non-blocking; proceeding on the recommended answers._
