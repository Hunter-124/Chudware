"""The `experimental.vision_review` serving seam (T-H10 DoD item 4).

Wires the offline VLM into the copilot's review/fix stages (the Seek-CAD split: the main
provider plans + emits ops; a SEPARATE local vision model judges the rendered viewport).
The model is served by llama.cpp with a vision projector (`--mmproj`) behind an
OpenAI-compatible loopback endpoint — the exact shape `config.example.json`'s
`experimental.vision_review` already describes, extended here with mmproj-aware fields.

Contract conformance:
  * Messages are built with the T-F5 providers/1 helpers (`text_part`, `image_part`, `user`
    from `services.agent.providers.base`) — an image is a `{"type":"image","path":..}` part,
    base64'd only at send time, exactly as the frozen providers/1 contract specifies.
  * The VLM proposes CONFIDENCE-TIERED claims JSON (the locked intake contract); it NEVER
    measures — deterministic/fiducial code verifies (T-F5 metrology owns measurement).
  * OFFLINE GUARD: when `offline_mode` is true the endpoint MUST be loopback (127.0.0.1 /
    localhost). A non-loopback base_url under offline_mode is refused, not silently allowed —
    the whole track is ITAR-safe local-only.

Default-OFF: `enabled` defaults false; omitting the block disables the seam entirely. This
module never downloads or launches a model — it talks to an already-running llama-server
(see `tools.flywheel.vlm.candidates.provision_instructions`).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

# providers/1 helpers (T-F5) — conform to the frozen message contract, do not re-invent it.
from services.agent.providers.base import text_part, image_part, user
from .candidate_eval import CLAIM_PROMPT, parse_claims, CONFIDENCE_TIERS

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

RENDER_REVIEW_PROMPT = (
    "This is a render of the CAD part just built. " + CLAIM_PROMPT +
    " Focus on whether the visible geometry matches a correct part: report the features you "
    "see (holes, bosses, fillets, bore) as coarse claims so deterministic code can verify "
    "them against the known model."
)


@dataclass(frozen=True)
class VisionReviewConfig:
    """Parsed `experimental.vision_review` block. `mmproj` and `model_path` are the T-H10
    mmproj-aware additions; when present they document which local GGUF+projector the
    endpoint is serving (llama-server is launched out-of-band with them)."""
    enabled: bool = False
    base_url: str = "http://localhost:1234/v1"
    model: str = "qwen3-vl-4b-instruct"
    mmproj: Optional[str] = None       # path to the mmproj-*.gguf the server was started with
    model_path: Optional[str] = None   # path to the model gguf (null when unprovisioned)
    offline_mode: bool = False

    @staticmethod
    def from_config(cfg: dict) -> "VisionReviewConfig":
        vr = (cfg.get("experimental") or {}).get("vision_review") or {}
        return VisionReviewConfig(
            enabled=bool(vr.get("enabled", False)),
            base_url=vr.get("base_url", "http://localhost:1234/v1"),
            model=vr.get("model", "qwen3-vl-4b-instruct"),
            mmproj=vr.get("mmproj"), model_path=vr.get("model_path"),
            offline_mode=bool(cfg.get("offline_mode", False)),
        )

    def _is_loopback(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        return host in _LOOPBACK_HOSTS

    def validate(self) -> None:
        """Enforce the offline loopback rule. Raises ValueError on a non-loopback endpoint
        under offline_mode — the seam must never phone home when the app is air-gapped."""
        if self.offline_mode and not self._is_loopback():
            raise ValueError(
                f"vision_review base_url {self.base_url!r} is not loopback but offline_mode "
                "is on; the vision endpoint must be local (127.0.0.1) when offline")


@dataclass
class VisionReviewResult:
    enabled: bool
    ok: bool
    claims: list[dict]
    reason: str = ""
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "ok": self.ok, "claims": self.claims,
                "reason": self.reason}


def _to_openai_messages(parts: list[dict], model: str) -> dict:
    """Convert providers/1 parts -> an OpenAI-compatible vision request. base64 the image
    ONLY here (send time), per the providers/1 contract note. Kept local so the same
    provider-neutral message can target any OpenAI-compatible server (llama.cpp, vLLM)."""
    import base64
    content = []
    for p in parts:
        if p["type"] == "text":
            content.append({"type": "text", "text": p["text"]})
        elif p["type"] == "image":
            with open(p["path"], "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            mime = p.get("mime", "image/png")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return {"model": model, "temperature": 0.0,
            "messages": [{"role": "user", "content": content}]}


def render_self_review(image_path: str, config: dict, *,
                       prompt: str = RENDER_REVIEW_PROMPT,
                       timeout_s: float = 120.0) -> VisionReviewResult:
    """Answer a render self-review call through the vision_review seam. Returns coarse,
    confidence-tiered claims about the rendered part for deterministic verification.

    Fully guarded: disabled seam -> ok=False/reason (never an error); offline non-loopback
    -> refused; endpoint down -> ok=False with the reason (the offline/CI reality), never a
    fabricated review. Builds its request with the providers/1 helpers so it stays
    contract-faithful."""
    vr = VisionReviewConfig.from_config(config)
    if not vr.enabled:
        return VisionReviewResult(enabled=False, ok=False, claims=[],
                                  reason="vision_review disabled (experimental, default-off)")
    try:
        vr.validate()
    except ValueError as e:
        return VisionReviewResult(enabled=True, ok=False, claims=[], reason=str(e))

    # providers/1 message → OpenAI vision request (T-F5 conformance).
    parts = user([text_part(prompt), image_part(image_path)])["content"]
    body = json.dumps(_to_openai_messages(parts, vr.model)).encode("utf-8")
    req = urllib.request.Request(vr.base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = resp["choices"][0]["message"]["content"]
    except Exception as e:  # endpoint down = the CI/offline reality — skip-with-reason
        return VisionReviewResult(
            enabled=True, ok=False, claims=[],
            reason=f"vision endpoint unreachable ({type(e).__name__}); serve a model first "
                   f"(see candidates.provision_instructions())")
    claims = parse_claims(text)
    # normalize confidence onto the intake tiers; default unlabeled to 'medium'
    for c in claims:
        if c.get("confidence") not in CONFIDENCE_TIERS:
            c["confidence"] = "medium"
    return VisionReviewResult(enabled=True, ok=True, claims=claims, raw=text[:800])
