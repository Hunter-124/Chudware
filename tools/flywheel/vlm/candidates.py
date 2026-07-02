"""The RATIFIED local candidate VLMs for the offline vision track (T-H10 Driver notes).

Model choices are LOCKED by Erik (pillar-16 decisions) — do NOT re-litigate; the research
record is `docs/VLM_OFFLINE_TRACK.md`. This module is a registry ONLY: it names the models,
their GGUF+mmproj artifacts, licenses, VRAM footprint, and primary task fit. It downloads
nothing and imports no ML library — provisioning is an explicit, foreman-authorized step
(see `provision_instructions`).

Every candidate here is Apache-2.0 (bundleable in a paid, ITAR-safe offline app) and runs
LOCALLY via llama.cpp with a vision projector (mmproj) — so once weights are on disk,
inference needs no network. That is the whole point of the "offline" track: the residual
these candidates score is measurable on an air-gapped box.

The two RULED-OUT models are recorded so a future reader does not reconsider them:
Qwen2.5-VL-3B (Qwen Research License — NON-COMMERCIAL) and Moondream 3 (BUSL-1.1) are OUT.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class VLMCandidate:
    """A local, Apache-2.0, llama.cpp-servable VLM candidate. All sizes are the MEASURED
    GGUF byte counts from `docs/VLM_OFFLINE_TRACK.md` §1 (unsloth/ibm-granite repo trees),
    not estimates, so `total_gb` is an honest disk/VRAM planning number."""
    key: str
    display_name: str
    hf_repo: str                 # base weights repo (Apache-2.0)
    gguf_repo: str               # GGUF + mmproj repo used by llama.cpp
    gguf_file: str               # quantized model gguf (Q4_K_M)
    mmproj_file: str             # vision projector gguf (mmproj-F16)
    params: str
    license: str                 # SPDX-ish; all Apache-2.0 here (bundleable)
    model_gb: float              # Q4_K_M model size
    mmproj_gb: float             # mmproj projector size
    context: str
    role: str                    # what this candidate is FOR in the track
    # Zero-shot literature floor (REFERENCE only — never a fabricated bench score). These
    # are published numbers from the cited sources; the real residual is MEASURED by
    # candidate_eval once provisioned. `None` where the survey flagged it UNVERIFIED.
    lit_floor: dict = field(default_factory=dict)
    unverified: tuple[str, ...] = ()   # open gaps to re-verify before any training claim

    @property
    def total_gb(self) -> float:
        return round(self.model_gb + self.mmproj_gb, 2)


# ---- the three RATIFIED candidates (LOCKED — do not add/remove without an Erik ruling) --

CANDIDATES: dict[str, VLMCandidate] = {
    "qwen3vl4b": VLMCandidate(
        key="qwen3vl4b",
        display_name="Qwen3-VL-4B-Instruct",
        hf_repo="Qwen/Qwen3-VL-4B-Instruct",
        gguf_repo="unsloth/Qwen3-VL-4B-Instruct-GGUF",
        gguf_file="Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
        mmproj_file="mmproj-F16.gguf",
        params="4.44B",
        license="Apache-2.0",
        model_gb=2.38,
        mmproj_gb=0.80,
        context="256K",
        role="PRIMARY — render self-review; render-vs-photo compare; photo feature triage. "
             "Exact class in the Unsloth GRPO notebooks (render-grounded GRPO turnkey).",
        # MechVQA (arXiv 2605.30794): mechanical-drawing zero-shot floor.
        lit_floor={"MechVQA_overall_pct": 60.2, "MechVQA_dim_annotation_pct": 77.0,
                   "DocVQA": 95.3, "ChartQA": 84.6, "OCRBench": 881,
                   "source": "docs/VLM_OFFLINE_TRACK.md §1,§3(b); tech-report Table 4"},
        unverified=("Qwen3-VL-2B bench numbers (2B not a candidate; 4B parsed w/ "
                    "±column-alignment caution)",),
    ),
    "granite41v4b": VLMCandidate(
        key="granite41v4b",
        display_name="granite-vision-4.1-4b",
        hf_repo="ibm-granite/granite-vision-4.1-4b",
        gguf_repo="ibm-granite/granite-vision-4.1-4b-GGUF",
        gguf_file="granite-vision-4.1-4b-Q4_K_M.gguf",
        mmproj_file="mmproj-model-f16.gguf",
        params="3.4B LLM + 0.6B vision",
        license="Apache-2.0",
        model_gb=2.40,   # Q4_K_M available per repo file list; planning figure
        mmproj_gb=0.60,
        context="128K",
        role="DRAWING SPECIALIST — chart/table/KVP extraction (94.2% VAREX KVP); the cheap "
             "dimension-callout reader. LoRA-native (rank-256 vision adapter).",
        lit_floor={"VAREX_KVP_pct": 94.2, "DocVQA": 94, "ChartQA": 87, "OCRBench": 790,
                   "source": "docs/VLM_OFFLINE_TRACK.md §1"},
    ),
    "gemma4e4b": VLMCandidate(
        key="gemma4e4b",
        display_name="Gemma 4 E4B-it",
        hf_repo="google/gemma-4-E4B-it",
        gguf_repo="ggml-org/gemma-4-E4B-it-GGUF",
        gguf_file="gemma-4-E4B-it-Q4_K_M.gguf",
        mmproj_file="mmproj-gemma-4-E4B-it-f16.gguf",
        params="4.5B eff / 8B total",
        license="Apache-2.0",
        model_gb=4.60,   # 8B raw weights; planning figure (~4-5 GB Q4)
        mmproj_gb=0.90,
        context="128K",
        role="HEDGE — Apache-2.0 fallback, day-0 llama.cpp. Dominated on drawing sheets "
             "(fixed 896x896 encoder poor for 2mm text) but kept as a license-clean hedge.",
        lit_floor={"OmniDocBench": 0.181,
                   "source": "docs/VLM_OFFLINE_TRACK.md §1 (std doc scores UNVERIFIED)"},
        unverified=("Gemma 4 E4B standard doc scores — run our own drawing eval",),
    ),
}

# Recorded so nobody reconsiders them (Driver notes: these are OUT).
RULED_OUT = {
    "Qwen2.5-VL-3B": "Qwen Research License — NON-COMMERCIAL only",
    "Moondream 3":   "BUSL-1.1 + use grant; cloud-only fine-tune; single-image; no llama.cpp",
}

PRIMARY_KEY = "qwen3vl4b"


def get(key: str) -> VLMCandidate:
    if key not in CANDIDATES:
        raise KeyError(f"unknown candidate {key!r}; known: {sorted(CANDIDATES)}")
    return CANDIDATES[key]


def provision_instructions(key: Optional[str] = None) -> str:
    """Exact, copy-pasteable commands to provision a candidate on a box with network +
    disk (NOT run in CI — provisioning is foreman/Erik-authorized). Returns the primary's
    instructions by default, or all three if key is 'all'."""
    keys = list(CANDIDATES) if key == "all" else [key or PRIMARY_KEY]
    lines = [
        "# Provision local VLM candidate(s) for the T-H10 zero-shot residual.",
        "# Prereq: llama.cpp built WITH vision (mmproj) support —",
        "#   git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp",
        "#   cmake -B build -DGGML_CUDA=ON && cmake --build build -j   # llama-server + llama-mtmd-cli",
        "# Qwen3-VL vision merged in PR#16780; Gemma 4 day-0; granite official GGUF+mmproj.",
        "pip install -U 'huggingface_hub[cli]'   # download only; NOT an in-process dep",
        "",
    ]
    for k in keys:
        c = CANDIDATES[k]
        lines += [
            f"# {c.display_name} ({c.license}, ~{c.total_gb} GB) — {c.role.splitlines()[0]}",
            f"huggingface-cli download {c.gguf_repo} {c.gguf_file} {c.mmproj_file} \\",
            f"    --local-dir ~/models/{c.key}",
            f"# serve (loopback, OpenAI-compatible, mmproj-aware):",
            f"llama-server -m ~/models/{c.key}/{c.gguf_file} \\",
            f"    --mmproj ~/models/{c.key}/{c.mmproj_file} --host 127.0.0.1 --port 1234",
            "",
        ]
    return "\n".join(lines)
