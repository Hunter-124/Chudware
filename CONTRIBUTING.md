# Contributing to Chudware

Thanks for your interest in improving Chudware. This guide covers how to propose changes,
the checks a change must pass, and the project's code and dependency rules. By
participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Issues

- Search existing issues before opening a new one.
- For bugs, include the version or commit, the platform, steps to reproduce, and what you
  expected versus what happened. A small `.step+` file or a screenshot helps.
- For feature requests, describe the workflow you are trying to accomplish.
- Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
  never in a public issue.

## Pull requests

1. Fork the repository and create a topic branch from the default branch.
2. Keep each pull request focused on one change. Large or cross-cutting changes (a new
   module, a contract change, a new dependency) are easier to land after a short design
   discussion in an issue.
3. Add or update tests for the behavior you change, and update the docs that describe it.
4. Run the checks below and make sure they pass.
5. Open the pull request with a description of what changed and why, and link the issue it
   addresses.

## Development setup

Follow [Building from source](README.md#building-from-source) to create the Python service
environment (`.venv-mcp`) and build the native targets. Requirements in short: CMake 3.24+,
Ninja, a C++20 toolchain (GCC/Clang on Linux, AppleClang on macOS, Visual Studio 2022 on
Windows), OCCT 7.9, and Python 3.13 (3.12 also works). C++17 is the baseline; the mesh,
manufacturing, twin, and print layers require C++20. CUDA is an optional LBM backend
(`CHUDWARE_WITH_CUDA`, default OFF). Windows specifics are in
[docs/reference/BUILD_WINDOWS.md](docs/reference/BUILD_WINDOWS.md).

Enable the repository's pre-commit hook once per clone:

```bash
git config core.hooksPath scripts/hooks
```

## Checks

```bash
.venv-mcp/bin/python scripts/check.py                            # fast source and audit gate
CHUDWARE_GATE_PYTHON=.venv-mcp/bin/python bash scripts/gate.sh   # full Linux gate
```

- `scripts/check.py` runs the deterministic source-level audits: GUI lint, ribbon command
  routing, SolidWorks-parity evidence pointers, agent event kinds, the no-Python-engine
  import ratchet, generated tool tables, advertised MCP tools, portable paths, and the
  shared-UI manifest. It needs no native build, but some audits import the MCP tool
  catalog, so run it with the service environment (`CHECK_PYTHON` selects another
  interpreter).
- `scripts/gate.sh` is the full Linux gate; its header comment is the authoritative stage
  list. It runs `check.py`, the core build and ctest, the GUI build, a WASM build smoke when
  emsdk is installed, the `chudware-hostd` lifecycle smoke and web-parity probe, the Python
  suites, and headless screenshot smokes. Stages whose environment is missing (no OCCT, no
  `xvfb-run`, no emsdk) report SKIP rather than failing; `CHUDWARE_GATE_STRICT=1` makes
  them fatal, as CI does.
- Targeted runs while iterating:
  - C++ core: `ctest --test-dir core/build-linux` (gate tree) or
    `ctest --test-dir build/<preset>/core` (preset tree).
  - Python: `.venv-mcp/bin/python -X utf8 -m pytest services/agent -q` (any package dir).
  - GUI: the headless screenshot harness, `CHUDWARE_SHOT=<scenario>` under `xvfb-run`.

Continuous integration runs the same checks on every pull request; see
[docs/reference/CI.md](docs/reference/CI.md).

## Code guidelines

- **New engine functionality lands in `core/` (C++)** behind the RPC surface. There is no
  Python CAD engine; `scripts/check.py`'s `oracle_import_audit` fails on any `engine.*` /
  `engine_oracle.*` import under `services/`, `scripts/`, or `tools/`. Python under
  `services/` is for the MCP server, the agent host and policy, and helpers with no C++
  counterpart.
- **Contracts first.** Interfaces between components are frozen in
  [contracts/](contracts/README.md). A new module ships its contract in the same change; a
  change to a frozen section bumps its version as described in `contracts/README.md`.
- **Keep the MCP surface consistent.** A change to an agent-facing tool, prompt, or
  workflow updates the server instructions in `services/mcp/server.py` and the matching
  `cad_guide` topics in the same change.
- **Match the surrounding code.** Follow the existing naming, comment density, and idioms
  of the file you are editing. Code, comments, and docs are in English.
- **Keep changes minimal.** A bug fix does not reformat or refactor unrelated code. Build
  what the change needs: no speculative options, extension points, or single-use
  abstractions. Delete dead code instead of commenting it out.
- **GUI code** lives in `host/`, the single GUI source tree for the native and WASM builds.
  Platform-specific behavior belongs behind build conditionals in the existing seams, not
  in a second GUI. Sizing and theming go through the shared metrics and theme tokens.
- Subsystem conventions and invariants are collected in [docs/dev/](docs/dev/README.md);
  some directories also carry a local `AGENTS.md` with notes for that subsystem.

## Dependencies and licensing

The authoritative dependency ledger is [contracts/licenses.md](contracts/licenses.md); open
compliance items are tracked in [contracts/license_debt.md](contracts/license_debt.md), and
the notices that ship with the app are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- **Ledger row before use.** Every new dependency (vendored code, Python package, CMake
  fetch, external binary, or model weights) gets a ledger row with its SPDX license, role,
  and posture in the same pull request. Prefer an existing in-repo capability over a new
  dependency.
- **In-process only when license-compatible.** Linked or imported dependencies must be
  permissively licensed, or LGPL linked dynamically (as for OCCT).
- **Copyleft tools stay out of process.** GPL and AGPL tools are invoked as user-installed
  subprocesses and are never linked, imported in-process, vendored, or bundled; when such a
  tool is missing, the feature that needs it degrades gracefully.
- **Excluded stays excluded.** A dependency the ledger marks excluded is not used.

If a dependency's license is unclear, raise it in the pull request before relying on it.

**Format reverse engineering must be clean-room.** Interoperability reverse engineering is
protected (for example under DMCA §1201(f) and Article 6 of the EU Software Directive) only
when no competitor source code, SDK, or EULA-restricted tooling is used. Do not contribute
code derived from such material.

## License of contributions

Chudware is licensed under the [MIT License](LICENSE). By submitting a contribution, you
agree that it is licensed under the same terms and that you have the right to submit it.
