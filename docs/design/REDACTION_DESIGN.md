# Redaction / restricted-data compliance

> **Design record; not a compliance certification or universal transport guarantee.**
> The wrapper described below is `services/agent/redaction.py::RedactingProvider`,
> applied by `services/agent/config.py::get_provider`. Its existence does not prove
> that every outbound path traverses it, nor that a regex scrub makes cloud submission
> of controlled data safe. Online agent turns follow
> [OMP integration](../dev/omp-integration.md). Audit the actual transport and
> data boundary before relying on this design for restricted-data handling.

This document describes the design for `compliance_mode` and the outbound
redaction pipeline in `services/agent/redaction.py`.

## Threat model

| Threat | v1 control |
|---|---|
| Exact dimensions / tolerances leave the machine in a cloud prompt | Deterministic GSL-aware scrub → stable placeholders |
| Material grades / alloy callouts in prompts | Material-spec regex → `MAT_n` |
| Part / drawing / project identifiers | PN / PROJ scrub |
| Screenshots of controlled geometry | **Block all outbound images** in `redact` mode |
| Residual free-text secrets after deterministic pass | Optional **loopback-only** local LLM flagger; failure → deterministic-only |
| Operator disables redaction but wants no cloud at all | `local_only` (and legacy `offline_mode`) refuse non-loopback providers |
| Redaction model itself exfiltrates | Redaction role **hard-refuses** non-loopback providers |
| Model echoes placeholders into tool calls | Inbound **rehydrate** before display / tool-dispatch |

Out of scope for v1: adversarial prompt-injection to exfiltrate the mapping
table (table never leaves process memory), multi-user shared sessions, or
cryptographic attestation of the redaction path.

## Modes (`config.json` → `compliance_mode`)

| Mode | Behavior |
|---|---|
| `off` (default) | No wrap. Existing `offline_mode` still enforced if set. |
| `redact` | Wrap resolved provider in `RedactingProvider`: scrub outbound, rehydrate inbound, `supports_vision=False`, drop image parts. Cloud providers allowed. |
| `local_only` | Refuse every non-loopback provider (reuses offline-mode assert path). No cloud. |

Legacy `offline_mode: true` remains the hard local-only switch and composes with
the above (`local_only_enabled` is true if either is set).

## Placeholder scheme

Per-session `RedactionMap` (lives on the wrapper instance):

| Category | Token | Examples matched |
|---|---|---|
| Dimensions / fits / tols | `DIM_n` | `100.5 mm`, `2.0 in`, `45°`, `±0.02 mm`, `+0.02/-0.01`, `H7`, `g6`, `H7/g6`, `Ø20 mm` |
| Materials | `MAT_n` | `6061-T6`, `AISI 4140`, `SS 304`, `Ti-6Al-4V`, `PEEK`, … |
| Part / drawing numbers | `PN_n` | `DWG-1234-A`, `P/N ABC-99-X` |
| Project / program ids | `PROJ_n` | `PRJ-FOO12`, `program id: AB-1234` |
| LLM residual spans | `SPAN_n` | Whatever the local flagger returns |

- **Stable across turns**: same original string → same token for the session.
- Mapping is **local only** (process memory). Never serialized to cloud, logs at
  `meta` consent, or sidecars.
- Rehydrate applies to assistant text, thinking, and tool-call argument trees.

## Pipeline

```
user / agent text (+ tool args)
        │
        ▼
 deterministic scrub ──► placeholders + local map
        │
        ▼
 optional local-LLM residual flag (loopback redaction role)
   on failure: log warning, keep deterministic result
        │
        ▼
 drop image parts (redact mode)
        │
        ▼
   cloud / inner provider.complete_ex
        │
        ▼
 rehydrate reply + thinking ──► agent loop / UI / tools
```

Seam: `config.get_provider` wraps with `redaction.wrap_provider` when mode is
`redact`, after offline/local-only gates. Wrapper preserves
`complete` / `complete_ex` / attribute proxy (`name`, `model`, `base_url`,
`offline_safe`); forces `supports_vision=False`.

## Redaction role

First-class role (`ai_roles.redaction` / `role_models.resolve_role_model`).
**Must** resolve to a loopback provider; cloud assignment is refused (hard).
If unresolved or non-loopback, v1 runs deterministic-only.

