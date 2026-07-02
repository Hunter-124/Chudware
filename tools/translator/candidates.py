"""Translator base-model candidates (T-F11 Stage-1; master plan §Pillar 13).

Two RATIFIED base models run in parallel; the eval harness picks the winner. Both are
license-clean text models (no vision — the translator's input is serialized GSL text).
Registry ONLY — downloads nothing, imports no ML library. Provisioning is out-of-band.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TranslatorCandidate:
    key: str
    display_name: str
    hf_repo: str
    gguf_repo: str
    gguf_file: str          # merged-LoRA serving target, Q4_K_M (~2.5 GB)
    params: str
    license: str
    context: str
    qlora_rank: str
    role: str
    notes: str = ""


CANDIDATES: dict[str, TranslatorCandidate] = {
    "qwen3_4b": TranslatorCandidate(
        key="qwen3_4b",
        display_name="Qwen3-4B-Instruct-2507",
        hf_repo="Qwen/Qwen3-4B-Instruct-2507",
        gguf_repo="Qwen/Qwen3-4B-Instruct-2507-GGUF",
        gguf_file="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        params="4B", license="Apache-2.0", context="262K", qlora_rank="16-32",
        role="PRIMARY — native tool calling, LM Studio day-0; QLoRA r=16-32 ~3-6 GB VRAM.",
        notes="Verify tokenizer digit handling before committing (subword tokenization "
              "degrades numeracy — master plan §Pillar 13).",
    ),
    "gemma3_4b": TranslatorCandidate(
        key="gemma3_4b",
        display_name="Gemma 3 4B-it",
        hf_repo="google/gemma-3-4b-it",
        gguf_repo="google/gemma-3-4b-it-qat-q4_0-gguf",
        gguf_file="gemma-3-4b-it-q4_0.gguf",
        params="4B", license="Gemma ToU (commercial OK, restrictions)", context="128K",
        qlora_rank="16-32",
        role="PARALLEL RUN — best-in-class IFEval, official QAT int4; matches existing local "
             "default. Eval gate picks the winner vs Qwen3-4B.",
    ),
}

RULED_OUT = {
    "Gemma 3n E4B": "'4B effective' only on Google's edge stack; llama.cpp quantizes all 8B "
                    "raw params, text-only",
    "Qwen2.5-3B": "non-commercial research license",
    "DiffusionGemma": "26B-MoE / 16.8 GB quantized, no server mode — watch, don't build on",
}

PRIMARY_KEY = "qwen3_4b"


def get(key: str) -> TranslatorCandidate:
    if key not in CANDIDATES:
        raise KeyError(f"unknown candidate {key!r}; known: {sorted(CANDIDATES)}")
    return CANDIDATES[key]


def provision_instructions(key: Optional[str] = None) -> str:
    """Copy-pasteable provisioning (NOT run in CI — foreman/Erik-authorized). Serves the
    merged-LoRA GGUF via llama.cpp on loopback, the drop-in for the existing local default."""
    keys = list(CANDIDATES) if key == "all" else [key or PRIMARY_KEY]
    lines = ["# Provision translator base model(s) for SFT + eval (out-of-band, never CI).",
             "pip install -U 'huggingface_hub[cli]'   # download only; not an in-process dep", ""]
    for k in keys:
        c = CANDIDATES[k]
        lines += [
            f"# {c.display_name} ({c.license}) — {c.role.splitlines()[0]}",
            f"huggingface-cli download {c.gguf_repo} {c.gguf_file} --local-dir ~/models/{c.key}",
            f"llama-server -m ~/models/{c.key}/{c.gguf_file} --host 127.0.0.1 --port 1234",
            "",
        ]
    return "\n".join(lines)
