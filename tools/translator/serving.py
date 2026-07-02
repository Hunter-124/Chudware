"""Local translator serving seam (T-F11 serving glue).

Wires the learned translator into the narration layer as an OPTION with a hard
deterministic-template fallback: build the GSL prompt -> ask the local model (served as
GGUF Q4_K_M via llama.cpp, the existing `LlamaCppProvider`) -> run the inference-time
groundedness OUTPUT GATE (`gate.narrate`) -> emit the model caption only if it is fully
grounded, else the deterministic `render_caption`. Never to fiction.

Provider-agnostic by design: `TranslatorNarrator` takes a `complete_fn(prompt) -> str`
callable, so it is testable offline with a mock and drops onto `LlamaCppProvider.complete`
in production. When no local translator is provisioned (the offline/CI reality) the narrator
degrades to templates — narration always succeeds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import gate
from .groundedness import GroundednessChecker, H4GroundednessAdapter
from .gsl_ser import serialize, SER_VERSION

# GBNF grammar constraining the model to grounded prose: the SER header tag, then
# "path is value." clauses. Numerals still verified post-hoc by the gate — the grammar
# shapes structure (and blocks free-form fiction), the gate enforces grounding.
GBNF_CAPTION_GRAMMAR = r'''
root      ::= header " " clause (" " clause)*
header    ::= "[" "gsl-ser/v1" " domain=" ident "]"
clause    ::= path " is " value "."
path      ::= ident ("." ident)*
ident     ::= [a-zA-Z_] [a-zA-Z0-9_]*
value     ::= number unit? | ident
number    ::= "-"? [0-9]+ ("." [0-9]+)?
unit      ::= "mm" | "deg" | "N" | "MPa" | "count" | "ratio" | "%"
'''

_PROMPT_TMPL = (
    "You narrate CAD state. Below is the canonical {ver} serialization. Write a faithful "
    "prose caption naming each fact. COPY every numeral verbatim from the serialization — "
    "never invent, round, or alter a number.{task}\n\n{ser}\n\nCaption:"
)


def build_prompt(state: dict, task: Optional[str] = None) -> str:
    """The SFT/serving prompt: the gsl-ser/v1 serialization (header tag in every prompt) +
    an optional task condition ('describe only what changed')."""
    task_line = f" Task: {task}." if task else ""
    return _PROMPT_TMPL.format(ver=SER_VERSION, task=task_line, ser=serialize(state))


@dataclass
class TranslatorNarrator:
    """Narration-layer option: local translator gated to deterministic-template fallback.

    `complete_fn(prompt) -> caption` is the served model (None = no local translator ->
    always template). `checker` is passed per-call to the gate (never mutates the global
    stub default)."""
    complete_fn: Optional[Callable[[str], str]] = None
    checker: Optional[GroundednessChecker] = None
    require_coverage: bool = False

    def narrate(self, state: dict, task: Optional[str] = None) -> tuple[str, str, gate.GateResult]:
        checker = self.checker or H4GroundednessAdapter()
        model_caption: Optional[str] = None
        if self.complete_fn is not None:
            try:
                model_caption = self.complete_fn(build_prompt(state, task))
            except Exception:
                model_caption = None            # any serving error -> safe template fallback
        return gate.narrate(state, model_caption, checker=checker,
                            require_coverage=self.require_coverage)


def from_llamacpp_provider(provider, *, checker: Optional[GroundednessChecker] = None,
                           system: str = "You are a precise CAD state narrator.",
                           require_coverage: bool = False) -> TranslatorNarrator:
    """Adapt an existing `services.agent.providers` Provider (e.g. LlamaCppProvider) into a
    narrator. Imported lazily by the caller to avoid a hard providers dependency here."""
    def _complete(prompt: str) -> str:
        return provider.complete(system, [{"role": "user",
                                           "content": [{"type": "text", "text": prompt}]}])
    return TranslatorNarrator(complete_fn=_complete, checker=checker,
                              require_coverage=require_coverage)
