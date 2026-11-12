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

For browser work, start with [web/README.md](web/README.md). The Compose path builds
the browser bundle, native engine and non-AI Python workers without the optional
online-agent SDK/runtime. Native provisioning uses the SHA-256-pinned SDK source
archive and runtime assets pinned in `vendor/omp/OMP_ARTIFACT.lock`. The official
[Oh My Pi source](https://github.com/can1357/oh-my-pi) is the upstream runtime;
keep immutable source revision and hash requirements intact. See
[runtime setup](README.md#optional-online-agent-setup) and the
[integration boundary](docs/dev/omp-integration.md).

Enable the repository's pre-commit hook once per clone:

```bash
git config core.hooksPath scripts/hooks
```

## Running tests

Tests may run directly on a developer host, including native Windows
(`scripts/test_lite.py --suite <name>`), or isolated in a container through
`scripts/testbox.py`. The root `conftest.py` points data roots at temp directories either
way, but a host run still shares the build trees, loopback ports and CPU with a live app:
use `CHUDWARE_ENGINE_DAEMON=0` (or a private daemon) for engine tests, and prefer testbox
for long or parallel runs.

Verify a change with the suite(s) for the area you touched (`--suite <name>[,<name>]`),
or let `scripts/testbox.py` select tests from your changes. Each suite stays under five
minutes warm; a suite run over that prints a warning, and a suite that keeps tripping it
warm gets split. Run the full gate (`scripts/testbox.py --gate`) only before a release or
when tracking down breakage across areas.

On a POSIX developer host, use `scripts/testbox.py` from the repository root. It
builds a Fedora 44 Podman image and runs against a private working-tree snapshot,
with separate cached build trees per slot:

```bash
scripts/testbox.py --list-suites
scripts/testbox.py --suite agent
scripts/testbox.py --suite geom,sketch --suite drawing
scripts/testbox.py --dry-run
scripts/testbox.py
scripts/testbox.py --gate
```

The default run selects tests from changes and has a 300-second budget; `--files`,
`--since` and `--budget` refine it. Named suites cover core and host CTest tests
and Python tests; selecting Python tests also builds the required app, hostd and
shape worker runtimes. The suites are `geom`, `sketch`, `drawing`, `assembly`,
`mesh_fea`, `sim`, `topopt`, `cam_mfg`, `ecad`, `formats`, `agent`, `parts`,
`platform`, `render` (renderer, viewport, camera, tessellation, screenshots), `gui`
(generic GUI shell, widgets, theme, icons) and `infra`. GUI tests live in the suite of
their product area. For every new test, assign its CTest name or pytest path
in `scripts/test_suites.json`: CMake rejects unassigned CTest tests and
`scripts/check.py` rejects unowned pytest files.

By default testbox chooses the first reachable SSH host with Podman and rsync from
the space-separated `CHUDWARE_TESTBOX_HOSTS` environment variable, or, when unset,
from `~/.config/chudware/testbox.json`:

```json
{"hosts": ["ssh-alias"]}
```

If none is available it uses local Podman. Everything testbox stores (slots, their
build trees, a private Podman image store and a ccache shared by all slots, so a fresh
slot reuses compiled objects) lives under `~/.cache/chudware-testbox`
on the execution host; symlink that directory onto a larger disk if needed. Hosts with
less than 30 GiB free there are skipped, and an explicit `--host` fails fast. Use
`--host local` to force local execution or `--host <ssh-alias>` to select a remote
host. Containers are capped at `nproc - 2` CPUs and run niced so the live app stays
responsive; `--jobs N` lowers parallelism further. Run logs are copied to
`.cache/testbox/<host>-<timestamp>/`. The runner
requires POSIX facilities and does not run directly under native Windows Python;
see the [Windows guide](docs/reference/BUILD_WINDOWS.md).

`scripts/testbox.py --gate` runs the authoritative full Linux gate, including source
audits, core tests by suite, GUI and runtime builds, Python suites and screenshot
smokes. Missing toolchains cause SKIPs; CI enables strict mode to make them fatal.
See the [CI workflow map](docs/reference/CI.md) for CI-specific coverage.

For source-only audits without a native build, run `.venv-mcp/bin/python
scripts/check.py` using the provisioned service environment; `CHECK_PYTHON` selects
another interpreter. Real systemd quota/isolation tests need deliberately provisioned
disposable quota roots and may skip without them; ordinary unit tests do not prove
kernel sandbox coverage.

Other checks:
  - Browser client: `./build.sh wasm` builds and stages the bundle and runs the asset
    parity check. It needs emsdk; the container build supplies its pinned toolchain.
  - Container or gateway behavior: use a disposable Compose project/volumes, never a
    running user's workspace. Exercise a real model operation and Python-backed
    operation, save/reopen across restart, and workspace upload/download. `/healthz`
    alone checks gateway liveness, not usable geometry.
    The executable local deployment probe is:

    ```bash
    .venv-mcp/bin/python web/deploy/smoke.py http://127.0.0.1:8300
    ```

    It checks real browser assets/security, native geometry/save/reopen/mesh and a
    general Python worker, creating/removing only its UUID-named smoke file. It does
    not start a deployment. For persistence, run once with
    `--keep --receipt /tmp/chudware-smoke.json` (choose a new receipt path), recreate
    the scratch deployment without deleting volumes, then run with
    `--resume /tmp/chudware-smoke.json` to reopen, verify and clean up. Do not point
    this local unauthenticated probe at an authenticated public proxy.

    For proxy changes, check
    unauthenticated HTTP and WebSocket denial, authenticated access to every route,
    same-origin enforcement, allowed hosts, and COOP/COEP headers. See the web guide's
    [health and troubleshooting](web/README.md#health-logs-and-troubleshooting).

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
