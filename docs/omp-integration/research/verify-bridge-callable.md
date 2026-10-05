# Bridge Feasibility: bun-callable `packages/ai` + `packages/catalog`

> **Historical research (2026-07), superseded 2026-08-31. Not a current runbook.** This dossier
> is dated evidence from the OMP integration research round. Its bun bridge/sidecar,
> `vendor/omp/` and `scripts/vendor_omp.py` were deleted. Any `scratch/omp-src/` or
> inspection-checkout path is an untracked local research copy, not a dependency. Its CAD
> file/line citations are point-in-time. Online agent execution, provider auth, models,
> sessions, memory and compaction now run in the embedded Chudcode RPC runtime. See
> [01-DECISIONS.md, 2026-08-31 section](../01-DECISIONS.md#2026-08-31--superseding-architecture-chudcode-rpc-is-the-only-online-runtime),
> [ARCHITECTURE.md, Agent runtime](../../../ARCHITECTURE.md#agent-runtime), and
> [`.claude/skills/cad-chudcode-integration/SKILL.md`](../../../.claude/skills/cad-chudcode-integration/SKILL.md).

## Verdict: **CONFIRMED** (stronger than the plan assumed)

The plan's premise — "stream → final-message" is callable from a standalone bun script by importing
`@oh-my-pi/pi-catalog` and `@oh-my-pi/pi-ai` directly from source — is **CONFIRMED end-to-end**, not
just at the construction/typecheck level. A throwaway `/tmp` script actually completed a real HTTPS
round-trip to `api.anthropic.com` and got back a well-formed `AssistantMessage` with
`stopReason: "error"` / `errorStatus: 401` (fake key rejected) rather than a thrown exception —
proving the whole stream machinery (dialect construction, HTTP client, event stream, `.result()`
resolution, error normalization) works standalone under bun with zero omp-src app-server bootstrap.

## Environment

- `bun --version` → `1.3.14` (matches `packageManager: "bun@1.3.14"` in root `package.json`).
- **No `node_modules` existed** prior to this verification (confirmed via glob — `node_modules/` is
  gitignored per `.gitignore:2`). Ran `bun install` from `/home/hunter/Repositories/CAD/scratch/omp-src`
  (483 packages, 2.05s) to materialize the workspace. This is **not a permanent repo change** —
  `node_modules/` is gitignored, so this only touched an untracked build artifact.

## Import resolution mechanism (CORRECTION to any assumption of explicit tsconfig `paths`)

- `tsconfig.base.json` has **no `compilerOptions.paths` mapping** for `@oh-my-pi/*` at all
  (`tsconfig.base.json:1-19` — only `target/module/lib/moduleResolution: "Bundler"/strict/types` etc.).
- Resolution instead comes purely from **bun workspaces + package.json `exports`**:
  - Root `package.json:12-16`: `"workspaces": {"packages": ["packages/*", "python/robomp/web"]}`.
  - `bunfig.toml:6`: `linker = "hoisted"` — after `bun install`, workspace packages are symlinked into
    the hoisted root `node_modules/@oh-my-pi/*` pointing back at `packages/ai` and `packages/catalog`.
  - Each package's `package.json` `"exports"` map resolves the bare specifier straight to TypeScript
    source (no build step needed): e.g. `packages/ai/package.json:59-63` — `"."` → `types`/`import`:
    `./src/index.ts`; `packages/catalog/package.json:53-57` same pattern.
  - So `import { getBundledModel } from "@oh-my-pi/pi-catalog"` resolves via node_modules symlink →
    `packages/catalog/package.json` `exports["."]` → `packages/catalog/src/index.ts`, executed directly
    as TS source by bun's native transpiler (no separate build/dist).

## Confirmed exports (source, not just graphify)

- `stream<TApi extends Api>(model, context, options?) => AssistantMessageEventStream` —
  `packages/ai/src/stream.ts:757`, re-exported via `packages/ai/src/index.ts:28` (`export * from "./stream"`).
- `AssistantMessageEventStream` class with `.result()` — defined in `packages/ai/src/utils/event-stream.ts`,
  re-exported via `packages/ai/src/index.ts:44`.
- `getBundledModel(provider, modelId) => Model<TApi>` — `packages/catalog/src/models.ts:32`, re-exported
  via `packages/catalog/src/index.ts:9` (`export * from "./models"`).
- `Context` type (`{systemPrompt, messages}`) — `packages/ai/src/types.ts`, re-exported via
  `packages/ai/src/index.ts:29`.

Graphify wasn't queried directly this round (source reads were fast and unambiguous); no disagreement
to flag since the source-of-truth import graph above was read directly.

## Minimal working bun snippet (verified to run)

```ts
import { getBundledModel } from "@oh-my-pi/pi-catalog";
import { stream } from "@oh-my-pi/pi-ai";
import type { Context } from "@oh-my-pi/pi-ai";

const model = getBundledModel("anthropic", "claude-sonnet-4-5");
const context: Context = {
  systemPrompt: "You are a test assistant.",
  messages: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
};

const events = stream(model, context, { apiKey: "sk-fake-not-a-real-key" });
const finalMessage = await events.result();
```

**Actual output** (run via `bun run /tmp/omp-bridge-verify.ts` from `scratch/omp-src`):
```
model resolved: true claude-sonnet-4-5 anthropic-messages
stream() returned: AssistantMessageEventStream
has result(): function
UNEXPECTED SUCCESS: {
  role: "assistant", content: [], api: "anthropic-messages", provider: "anthropic",
  model: "claude-sonnet-4-5", stopReason: "error", errorStatus: 401,
  errorMessage: "401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\",...}}",
  duration: 286.3,
}
```
Note: `.result()` did **not throw** — errors are captured into the resolved `AssistantMessage` as
`stopReason: "error"` + `errorStatus`/`errorMessage` fields. Any bridge/host code awaiting `.result()`
must check `stopReason === "error"` rather than rely on try/catch to detect provider failures (though
truly network-level failures, e.g. DNS/connect refused, likely do throw — not exercised here since the
real Anthropic endpoint was reachable).

## Transitive dependency list (import-time, from `packages/ai/src/index.ts` + `packages/catalog/src/index.ts`)

Direct workspace deps (declared in each `package.json`):
- `packages/ai` (`packages/ai/package.json:39-46`): `@bufbuild/protobuf`, `@oh-my-pi/pi-catalog`,
  `@oh-my-pi/pi-utils`, `@oh-my-pi/pi-wire`, `arktype`, `zod`.
- `packages/catalog` (`packages/catalog/package.json:35-40`): `@bufbuild/protobuf`, `@oh-my-pi/pi-utils`,
  `arktype`, `zod`.

Re-exported third-party at the `pi-ai` barrel (`packages/ai/src/index.ts:1-2`): `arktype` (`type`, `Type`),
`zod/v4` (`z`, `ZodType`).

**Bun-only / native APIs pulled in transitively at import time:**
- `bun:sqlite` — `packages/ai/src/auth-storage.ts:10` (`import { Database, type Statement } from "bun:sqlite"`)
  and `packages/catalog/src/model-cache.ts:5` (`import { Database } from "bun:sqlite"`). Both are reached
  because `packages/ai/src/index.ts:8` does `export * from "./auth-storage"` and
  `packages/catalog/src/index.ts:6` does `export * from "./model-cache"`, i.e. **`bun:sqlite` loads
  unconditionally as soon as either package barrel is imported** — this only runs under the bun runtime,
  not plain Node, and is the single hardest bun-lock-in point for any host embedding these packages.
- `@oh-my-pi/pi-utils`, `@oh-my-pi/pi-wire` — pulled in via workspace symlinks the same way as `pi-ai`/
  `pi-catalog` (own `exports` maps to `src/index.ts`); not inspected line-by-line this round but resolved
  cleanly during the live run (no missing-module errors).
- `@oh-my-pi/pi-natives*` — bun install additionally resolved platform-specific native packages
  (`@oh-my-pi/pi-natives-linux-x64` was fetched during install) as an optional/transitive dependency of
  the workspace closure, though not directly imported by `pi-ai`/`pi-catalog` themselves in the exercised
  path.

## Corrections to plan assumptions

1. **No explicit tsconfig `paths` mapping exists** for `@oh-my-pi/*` — resolution is 100% bun-workspace
   `node_modules` symlink + package `exports`, not a manual path alias. Any embedding strategy that
   doesn't run through `bun install` (or replicate its symlink layout) will fail to resolve these imports.
2. **`bun install` is a hard prerequisite**, not optional — `node_modules` did not pre-exist in the
   vendored tree. This is a one-time, gitignored, non-permanent step (483 packages, ~2s).
3. **`.result()` resolves rather than throws** on provider HTTP errors (401 in this test) — the plan's
   assumption of "fails at fetch, capture the error" holds only for genuine network-level failures; HTTP-level
   provider errors are captured as data on the resolved message, which is arguably *friendlier* for a bridge
   (no need to wrap every call in try/catch to detect auth/rate-limit failures).
4. **`bun:sqlite` is an unconditional transitive import** of both package barrels (via `auth-storage.ts`
   and `model-cache.ts`), confirming any OMP↔CAD bridge process embedding these packages must run under
   the real `bun` runtime — it cannot be transpiled/run under plain Node or a non-bun JS engine without
   stubbing `bun:sqlite`.

Cleanup: `/tmp/omp-bridge-verify.ts` removed after the run; no permanent changes made to `scratch/omp-src`
(node_modules is gitignored and was not part of any commit).
