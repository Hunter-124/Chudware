# Experimental AI features

Opt-in, **default-off** additions to the AI copilot, drawn from a 2026 research pass on alternative
models/providers and generative-CAD techniques. Everything here is designed to run **fully local**
(open-weight, self-hostable) so it is **ITAR-safe** — the Alibaba/DeepSeek *cloud* APIs are not used;
only their open weights, run on your own OpenAI-compatible server (LM Studio / Ollama / vLLM).

All features are **additive**: with them off, the resolved provider descriptor and the agent run loop
are byte-identical to the non-experimental path, so the Python-parity gate (`test_agent`) is unchanged.

Configure them in **Settings → Experimental**, or directly in `%APPDATA%/AI-CAD/config.json` under the
`"experimental"` block (see `config.example.json`).

---

## 1. Open-weight local model presets

The `openai` provider already speaks the OpenAI-compatible wire format, so the research's recommended
open-weight models run through it unchanged — they are exposed as **one-click presets**, not a new
protocol.

**Where:** Settings → AI → Provider = *Local (OpenAI-compatible)* → **Quick preset**.

| Preset | Role | Notes |
|---|---|---|
| Qwen2.5-Coder 7B / 32B | main agent (op/code emission) | Reliable JSON + tool-calling for CAD ops |
| Qwen2.5 14B | planner / intake | General reasoning, no vision head |
| Qwen2.5-VL 7B | vision review judge | Beat Gemini 1.5 Pro on grounding in the research |
| Qwen2.5-VL 3B | vision (light) | Smaller GPUs |
| DeepSeek-R1 Distill 32B | reasoning loop | Q4 fits a single 24 GB GPU |

A preset fills the model id, vision flag, and a default base URL. **You still need the weights loaded
in your local server.** These run fully local, so they are allowed under **Offline (ITAR) mode**.

### Cloud APIs (Qwen / DeepSeek) — non-ITAR

For when ITAR mode is **off**, there's also a **Cloud API (Qwen / DeepSeek / OpenAI-compatible)**
provider (Settings → AI → Provider). It resolves as an `openai` provider with a cloud `base_url`, so:

- **Alibaba Cloud (DashScope)** — `qwen-max`, `qwen-plus`, `qwen-vl-max`, `qwen-coder-plus`
- **DeepSeek API** — `deepseek-chat`, `deepseek-reasoner`

Pick a preset (it fills the service endpoint + model), paste your API key, and go. **These are network
providers and are automatically refused when Offline (ITAR) mode is on** — the offline guard in
`resolve_provider` rejects any non-loopback endpoint before a single byte leaves the machine, while the
local presets above keep working. That's the whole policy: local open-weight = ITAR-OK, Alibaba/DeepSeek
*cloud* = ITAR-blocked.

---

## 2. Local vision review (the "Seek-CAD split")

Normally the review/fix stages reuse the **main** provider for vision. With this on, a **separate local
vision model** (e.g. self-hosted Qwen2.5-VL) judges the rendered viewport, while the main provider
keeps planning and emitting ops. This enables:

- **strong/cloud planner + local vision judge**, or
- a **fully-local self-correction loop** for ITAR.

**Where:** Settings → Experimental → *Local vision review*.

**Config:**
```json
"experimental": {
  "vision_review": {
    "enabled": true,
    "base_url": "http://localhost:1234/v1",
    "model": "qwen2.5-vl-7b-instruct"
  }
}
```

**How it's wired:** the host writes the block; the C++ core's `apply_experimental()`
(`core/agent/config.cpp`) resolves it via the same `resolve_provider()` path (so the offline guard
still applies — a cloud review endpoint is **refused while offline**, falling back to the main local
provider rather than leaking egress). `run_design()` (`core/agent/run.cpp`) builds a second
`ProviderFn` used only by the **review** and **fix** stages; its own context/output budget is fitted to
that model's window. Absent/disabled → review uses the main provider exactly as before.

---

## 3. Parametric sequence mode (DeepCAD / CAD-Coder direction)

Steers the modeler to produce an **ordered sequence of individually editable features** (sketch →
extrude/revolve/loft/sweep → fillet/chamfer/hole/pattern) that reconstructs a clean design history —
rather than one fused/imported solid. This keeps AI-generated parts editable as a real feature tree,
the core idea behind the DeepCAD / CAD-Coder / GenCAD command-sequence research.

**Where:** Settings → Experimental → *Parametric sequence mode*.

**Config:** `"experimental": { "cad_sequence_mode": true }`

**Status:** today this is **prompt-level steering** — a run-scoped directive appended to the modeler
context (`cad_seq_directive` in `run_design()`). A dedicated sequence-generation model (training a
DeepCAD/CAD-Coder-style head that emits command tokens compiled to B-rep via the existing OCCT kernel)
is the natural next step and is tracked as future work.

---

## Research provenance

See the deep-research synthesis that motivated these (Qwen2.5-VL grounding vs Gemini; the
DeepCAD→GenCAD→CAD-Coder→Seek-CAD lineage emitting editable command sequences compiled to B-rep via
PythonOCC/OCCT — the same kernel AI-CAD uses). Key sources: Qwen2.5-VL technical report
(arXiv 2502.13923), CAD-Coder (arXiv 2505.14646), Seek-CAD (arXiv 2505.17702), GenCAD (arXiv 2409.16294).
