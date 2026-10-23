# SPDX-License-Identifier: MIT AND CC-BY-NC-4.0
#
# The model classes ``FourierPointEncoder`` and ``CADRecode`` in this file are derived
# from filaPro/cad-recode (https://github.com/filaPro/cad-recode) and remain under the
# Creative Commons Attribution-NonCommercial 4.0 International license (CC-BY-NC-4.0);
# they are not covered by Chudware's MIT license. The rest of this file is MIT-licensed.
"""CAD-Recode v1.5 ML parametric-rebuild backend.

Translates a mesh / point cloud into executable CadQuery source via the
HuggingFace weights ``filapro/cad-recode-v1.5`` (Qwen2-1.5B + a single linear
point encoder), then materialises the source to STEP in a sandboxed
subprocess.

Model classes (``FourierPointEncoder``, ``CADRecode``) are ported from the
upstream demo notebook:

    https://github.com/filaPro/cad-recode/blob/main/demo.ipynb

The ported classes and the weights are CC-BY-NC-4.0 (non-commercial use only), a documented
exception to the project license; see ``contracts/license_debt.md`` item 19. ALL heavy
imports (torch, transformers, trimesh, cadquery, pytorch3d) are lazy — importing this
module is dep-free.

Public API::

    build_parametric(mesh_path, session_dir, scale_transform=None, device=None)
        -> {ok, cadquery_src, step_path, error, provenance}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_ID = "filapro/cad-recode-v1.5"
TOKENIZER_ID = "Qwen/Qwen2-1.5B"
PROVENANCE = {"method": "ml_cad_recode", "model": MODEL_ID}

# Upstream demo divides CQ mesh vertices by 100 to land in the normalised
# unit-cube space (v1.5 integer range is -100…+100). See demo.ipynb cell 14.
CQ_UNIT_SCALE = 100.0

N_PRE_POINTS = 8192
N_POINTS = 256
MAX_NEW_TOKENS = 768
DEFAULT_TIMEOUT_S = 30.0

ENV_WEIGHTS = "CAD_RECODE_WEIGHTS"


# ---------------------------------------------------------------------------
# Provisioning / readiness (no heavy imports)
# ---------------------------------------------------------------------------

def _weights_candidates() -> list[Path]:
    """Offline candidate paths for the CAD-Recode checkpoint."""
    out: list[Path] = []
    env = os.environ.get(ENV_WEIGHTS, "").strip()
    if env:
        out.append(Path(env))
    home = Path.home()
    out.extend(
        [
            home / ".cache" / "huggingface" / "hub" / "models--filapro--cad-recode-v1.5",
            home / ".cache" / "cad_recode",
            Path("/opt/models/cad-recode-v1.5"),
        ]
    )
    return out


def weights_path() -> Optional[str]:
    """Return a resolvable weights path, or None if nothing is on disk."""
    for p in _weights_candidates():
        if p.is_file() or p.is_dir():
            return str(p)
    return None


def _torch_importable() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def is_provisioned() -> bool:
    """True when torch is importable AND weights are discoverable offline."""
    return _torch_importable() and weights_path() is not None


def not_provisioned_error() -> str:
    return (
        "not provisioned: CAD-Recode requires torch + transformers and the "
        f"{MODEL_ID} weights. Install deps, then fetch weights with: "
        f"python -c \"from huggingface_hub import snapshot_download; "
        f"snapshot_download('{MODEL_ID}')\" "
        f"(or set {ENV_WEIGHTS} to a local snapshot). "
        "VRAM: ~4 GB bf16 on GPU (1.5B); CPU fallback is supported but slow."
    )


# ---------------------------------------------------------------------------
# Preprocess: mesh → 256 FPS points + inverse transform
# ---------------------------------------------------------------------------

def _fps_numpy(points, k: int, seed: Optional[int] = None):
    """Greedy farthest-point sampling (numpy). Deterministic given seed.

    Starts from a seeded random index (or 0 when seed is None) so tests can
    pin the first point. Returns indices into ``points``.
    """
    import numpy as np

    pts = np.asarray(points, dtype=np.float64)
    n = pts.shape[0]
    if n == 0:
        raise ValueError("empty point set")
    k = min(k, n)
    if seed is None:
        start = 0
    else:
        rng = np.random.default_rng(seed)
        start = int(rng.integers(0, n))
    selected = np.empty(k, dtype=np.int64)
    selected[0] = start
    # Squared distances to the selected set
    dist = np.full(n, np.inf, dtype=np.float64)
    for i in range(1, k):
        last = pts[selected[i - 1]]
        d = np.sum((pts - last) ** 2, axis=1)
        dist = np.minimum(dist, d)
        selected[i] = int(np.argmax(dist))
    return selected


def _fps(points, k: int, seed: Optional[int] = None):
    """FPS via pytorch3d when available, else numpy fallback.

    pytorch3d is OPTIONAL. When present we still force a deterministic start
    (random_start_point=False) so seed only matters for the numpy path's
    first-point choice; both paths return index arrays of length k.
    """
    import numpy as np

    pts = np.asarray(points, dtype=np.float32)
    try:
        import torch
        from pytorch3d.ops import sample_farthest_points

        t = torch.as_tensor(pts).unsqueeze(0)  # (1, N, 3)
        # random_start_point=False → start at index 0 (deterministic).
        _, ids = sample_farthest_points(t, K=k, random_start_point=False)
        return ids[0].cpu().numpy()
    except Exception:  # noqa: BLE001 — missing pytorch3d or torch
        return _fps_numpy(pts, k, seed=seed)


def compute_normalize_transform(mesh) -> dict[str, Any]:
    """Return {center: [x,y,z], scale: float} so max-extent maps to 2 at origin.

    Inverse: ``p_metric = p_norm / scale + center``.
    """
    import numpy as np

    bounds = mesh.bounds  # (2, 3)
    center = ((bounds[0] + bounds[1]) / 2.0).tolist()
    extents = bounds[1] - bounds[0]
    max_ext = float(np.max(extents))
    if max_ext <= 0:
        scale = 1.0
    else:
        scale = 2.0 / max_ext
    return {"center": [float(c) for c in center], "scale": float(scale)}


def apply_normalize(points, transform: dict[str, Any]):
    """Apply recorded normalize: (p - center) * scale."""
    import numpy as np

    pts = np.asarray(points, dtype=np.float64)
    center = np.asarray(transform["center"], dtype=np.float64)
    scale = float(transform["scale"])
    return (pts - center) * scale


def apply_inverse(points, transform: dict[str, Any]):
    """Inverse of :func:`apply_normalize`: p / scale + center."""
    import numpy as np

    pts = np.asarray(points, dtype=np.float64)
    center = np.asarray(transform["center"], dtype=np.float64)
    scale = float(transform["scale"])
    if scale == 0:
        return pts + center
    return pts / scale + center


def preprocess_mesh(
    mesh_path: str,
    *,
    scale_transform: Optional[dict] = None,
    n_pre_points: int = N_PRE_POINTS,
    n_points: int = N_POINTS,
    seed: Optional[int] = 0,
) -> dict[str, Any]:
    """Mesh → surface sample → FPS → normalised 256-pt cloud.

    Returns::

        {
          "points": ndarray (n_points, 3) float32 in normalised space,
          "scale_transform": {"center": [x,y,z], "scale": float},
        }

    The inverse of ``scale_transform`` returns geometry to metric scene units.
    CadQuery integer units still need the extra ``/ CQ_UNIT_SCALE`` (handled
    in the materialise runner).
    """
    import numpy as np
    import trimesh

    mesh = trimesh.load_mesh(mesh_path, force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    if mesh is None or getattr(mesh, "vertices", None) is None or len(mesh.vertices) == 0:
        raise ValueError(f"empty or unreadable mesh: {mesh_path}")

    transform = scale_transform or compute_normalize_transform(mesh)
    # Sample on the raw mesh, then normalise the samples (equivalent to the
    # upstream demo's "normalise mesh then sample" for affine transforms).
    vertices, _ = trimesh.sample.sample_surface(mesh, n_pre_points)
    ids = _fps(vertices, n_points, seed=seed)
    pts = np.asarray(vertices[ids], dtype=np.float64)
    pts = apply_normalize(pts, transform).astype(np.float32)
    return {"points": pts, "scale_transform": transform}


# ---------------------------------------------------------------------------
# Model classes (ported from upstream demo.ipynb — lazy torch)
# ---------------------------------------------------------------------------

def _build_model_classes():
    """Import torch/transformers and return (FourierPointEncoder, CADRecode).

    Ported from https://github.com/filaPro/cad-recode/blob/main/demo.ipynb
    """
    import torch
    from torch import nn
    from transformers import Qwen2ForCausalLM, Qwen2Model, PreTrainedModel
    from transformers.modeling_outputs import CausalLMOutputWithPast

    class FourierPointEncoder(nn.Module):
        def __init__(self, hidden_size):
            super().__init__()
            frequencies = 2.0 ** torch.arange(8, dtype=torch.float32)
            self.register_buffer("frequencies", frequencies, persistent=False)
            self.projection = nn.Linear(51, hidden_size)

        def forward(self, points):
            x = points
            x = (x.unsqueeze(-1) * self.frequencies).view(*x.shape[:-1], -1)
            x = torch.cat((points, x.sin(), x.cos()), dim=-1)
            x = self.projection(x)
            return x

    class CADRecode(Qwen2ForCausalLM):
        def __init__(self, config):
            PreTrainedModel.__init__(self, config)
            self.model = Qwen2Model(config)
            self.vocab_size = config.vocab_size
            self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

            torch.set_default_dtype(torch.float32)
            self.point_encoder = FourierPointEncoder(config.hidden_size)
            torch.set_default_dtype(torch.bfloat16)

        def forward(
            self,
            input_ids=None,
            attention_mask=None,
            point_cloud=None,
            position_ids=None,
            past_key_values=None,
            inputs_embeds=None,
            labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            cache_position=None,
        ):
            output_attentions = (
                output_attentions
                if output_attentions is not None
                else self.config.output_attentions
            )
            output_hidden_states = (
                output_hidden_states
                if output_hidden_states is not None
                else self.config.output_hidden_states
            )
            return_dict = (
                return_dict if return_dict is not None else self.config.use_return_dict
            )

            # concatenate point and text embeddings
            if past_key_values is None or past_key_values.get_seq_length() == 0:
                assert inputs_embeds is None
                inputs_embeds = self.model.embed_tokens(input_ids)
                point_embeds = self.point_encoder(point_cloud).bfloat16()
                inputs_embeds[attention_mask == -1] = point_embeds.reshape(
                    -1, point_embeds.shape[2]
                )
                attention_mask[attention_mask == -1] = 1
                input_ids = None
                position_ids = None

            outputs = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                cache_position=cache_position,
            )

            hidden_states = outputs[0]
            logits = self.lm_head(hidden_states)
            logits = logits.float()

            loss = None
            if labels is not None:
                shift_logits = logits[..., :-1, :].contiguous()
                shift_labels = labels[..., 1:].contiguous()
                loss_fct = torch.nn.CrossEntropyLoss()
                shift_logits = shift_logits.view(-1, self.config.vocab_size)
                shift_labels = shift_labels.view(-1)
                shift_labels = shift_labels.to(shift_logits.device)
                loss = loss_fct(shift_logits, shift_labels)

            if not return_dict:
                output = (logits,) + outputs[1:]
                return (loss,) + output if loss is not None else output

            return CausalLMOutputWithPast(
                loss=loss,
                logits=logits,
                past_key_values=outputs.past_key_values,
                hidden_states=outputs.hidden_states,
                attentions=outputs.attentions,
            )

        def prepare_inputs_for_generation(self, *args, **kwargs):
            model_inputs = super().prepare_inputs_for_generation(*args, **kwargs)
            model_inputs["point_cloud"] = kwargs["point_cloud"]
            return model_inputs

    return FourierPointEncoder, CADRecode


# Module-level cache so repeated calls in one process don't re-load weights.
_MODEL_CACHE: dict[str, Any] = {}


def _resolve_device(device: Optional[str]) -> str:
    if device:
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def _attn_implementation(device: str) -> Optional[str]:
    if not device.startswith("cuda"):
        return None
    try:
        import flash_attn  # noqa: F401

        return "flash_attention_2"
    except Exception:  # noqa: BLE001
        return "eager"


def load_model(device: Optional[str] = None):
    """Load tokenizer + CADRecode weights (cached). Lazy heavy imports."""
    import torch
    from transformers import AutoTokenizer

    dev = _resolve_device(device)
    cache_key = f"{MODEL_ID}::{dev}"
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    _, CADRecode = _build_model_classes()
    attn = _attn_implementation(dev)
    tokenizer = AutoTokenizer.from_pretrained(
        TOKENIZER_ID,
        pad_token="<|im_end|>",
        padding_side="left",
    )
    wpath = weights_path() or MODEL_ID
    load_kwargs: dict[str, Any] = {
        "torch_dtype": "auto",
    }
    if attn is not None:
        load_kwargs["attn_implementation"] = attn
    try:
        model = CADRecode.from_pretrained(wpath, **load_kwargs)
    except Exception:
        # flash-attn path can fail on older GPUs — retry eager
        if load_kwargs.get("attn_implementation") == "flash_attention_2":
            load_kwargs["attn_implementation"] = "eager"
            model = CADRecode.from_pretrained(wpath, **load_kwargs)
        else:
            raise
    model = model.eval().to(dev)
    bundle = {"tokenizer": tokenizer, "model": model, "device": dev}
    _MODEL_CACHE[cache_key] = bundle
    return bundle


def generate_cadquery(
    points,
    *,
    device: Optional[str] = None,
    max_new_tokens: int = MAX_NEW_TOKENS,
) -> str:
    """Run CAD-Recode on a normalised (N,3) point cloud → CadQuery source."""
    import numpy as np
    import torch

    bundle = load_model(device=device)
    tokenizer = bundle["tokenizer"]
    model = bundle["model"]
    dev = bundle["device"]

    pts = np.asarray(points, dtype=np.float32)
    n = pts.shape[0]
    pad_id = tokenizer.pad_token_id
    im_start = tokenizer("<|im_start|>")["input_ids"][0]
    input_ids = [pad_id] * n + [im_start]
    attention_mask = [-1] * n + [1]

    with torch.no_grad():
        batch_ids = model.generate(
            input_ids=torch.tensor(input_ids).unsqueeze(0).to(dev),
            attention_mask=torch.tensor(attention_mask).unsqueeze(0).to(dev),
            point_cloud=torch.tensor(pts).unsqueeze(0).to(dev),
            max_new_tokens=max_new_tokens,
            pad_token_id=pad_id,
        )
    py_string = tokenizer.batch_decode(batch_ids)[0]
    begin = py_string.find("<|im_start|>")
    if begin < 0:
        begin = 0
    else:
        begin = begin + len("<|im_start|>")
    end = py_string.find("<|endoftext|>")
    if end < 0:
        end = len(py_string)
    return py_string[begin:end].strip()


# ---------------------------------------------------------------------------
# Subprocess CadQuery materialisation (never exec model output in-process)
# ---------------------------------------------------------------------------

def _runner_payload_path_arg() -> str:
    """Marker argv flag for the sandboxed runner entry point."""
    return "--cad-recode-exec"


def materialize_cadquery(
    cadquery_src: str,
    session_dir: str,
    scale_transform: dict[str, Any],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    step_name: str = "cad_recode.step",
) -> dict[str, Any]:
    """Exec CadQuery source in a subprocess; export scaled STEP.

    Never executes model output in-process. Returns
    ``{ok, step_path, error}``.
    """
    out_dir = Path(session_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    step_path = out_dir / step_name

    payload = {
        "code": cadquery_src,
        "step_path": str(step_path),
        "scale_transform": scale_transform,
        "cq_unit_scale": CQ_UNIT_SCALE,
    }

    # Write payload to a temp file (avoids argv length / stdin encoding issues).
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="cad_recode_payload_",
        delete=False,
        encoding="utf-8",
    ) as fh:
        json.dump(payload, fh)
        payload_path = fh.name

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "services.vision.cad_recode",
                _runner_payload_path_arg(),
                payload_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
    except subprocess.TimeoutExpired:
        try:
            os.unlink(payload_path)
        except OSError:
            pass
        return {
            "ok": False,
            "step_path": None,
            "error": f"CadQuery materialise timed out after {timeout_s:.0f}s",
        }
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    # Prefer the last JSON line on stdout.
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        return {
            "ok": False,
            "step_path": None,
            "error": (
                f"CadQuery runner produced no output (rc={proc.returncode}). "
                f"stderr={stderr[:500]}"
            ),
        }
    try:
        data = json.loads(stdout.splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "step_path": None,
            "error": f"Bad runner output: {exc}; stdout={stdout[:500]}; stderr={stderr[:300]}",
        }
    if not isinstance(data, dict):
        return {"ok": False, "step_path": None, "error": "runner returned non-dict"}
    return {
        "ok": bool(data.get("ok")),
        "step_path": data.get("step_path"),
        "error": data.get("error"),
    }


def _exec_runner(payload_path: str) -> int:
    """Subprocess entry: exec CadQuery source, export STEP with inverse scale.

    Expects the model code to bind a global ``r`` (CadQuery Workplane / shape).
    Applies CQ-unit → normalised (``/ cq_unit_scale``) then inverse of the
    recorded mesh normalise so the STEP lands in metric scene units (mm if the
    input mesh was mm).

    User code is exec'd before export so a hanging snippet times out even when
    cadquery is not installed (parent enforces the timeout).
    """
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        code = payload["code"]
        step_path = payload["step_path"]
        transform = payload.get("scale_transform") or {
            "center": [0.0, 0.0, 0.0],
            "scale": 1.0,
        }
        cq_unit = float(payload.get("cq_unit_scale", CQ_UNIT_SCALE))

        glb: dict[str, Any] = {}
        cq = None
        try:
            import cadquery as _cq

            cq = _cq
            glb["cq"] = _cq
            glb["cadquery"] = _cq
        except Exception:  # noqa: BLE001
            pass

        # Upstream model emits bare CadQuery scripts that expect `cq` + bind `r`.
        # Exec first so hang/timeout is enforced even without cadquery installed.
        exec(code, glb, glb)  # noqa: S102 — intentional, sandboxed process

        if cq is None:
            raise RuntimeError(
                "cadquery is not installed in this environment "
                "(pip install cadquery)"
            )
        if "r" not in glb:
            raise RuntimeError("CadQuery source did not bind global 'r'")
        r = glb["r"]
        compound = r.val() if hasattr(r, "val") else r

        center = transform.get("center") or [0.0, 0.0, 0.0]
        scale = float(transform.get("scale") or 1.0)
        # CQ integer units → normalised, then inverse-normalise to metric.
        # p_metric = (p_cq / cq_unit) / scale + center
        metric_scale = 1.0 / (cq_unit * scale) if scale != 0 else 1.0 / cq_unit
        try:
            compound = compound.scale(metric_scale)
        except Exception:
            # Some CQ versions expose scale on the workplane result differently.
            compound = (
                compound.val().scale(metric_scale)
                if hasattr(compound, "val")
                else compound
            )
        try:
            compound = compound.translate(
                (float(center[0]), float(center[1]), float(center[2]))
            )
        except Exception:
            pass

        Path(step_path).parent.mkdir(parents=True, exist_ok=True)
        cq.exporters.export(compound, step_path)
        if not Path(step_path).is_file():
            raise RuntimeError(f"STEP export produced no file at {step_path}")
        sys.stdout.write(
            json.dumps({"ok": True, "step_path": step_path, "error": None}) + "\n"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "step_path": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-800:],
                }
            )
            + "\n"
        )
        return 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_parametric(
    mesh_path: str,
    session_dir: str,
    scale_transform: Optional[dict] = None,
    device: Optional[str] = None,
) -> dict[str, Any]:
    """ML parametric rebuild: mesh → CadQuery source → scaled STEP.

    Parameters
    ----------
    mesh_path:
        Path to an input mesh (STL/OBJ/PLY/…).
    session_dir:
        Directory for the exported STEP (created if missing).
    scale_transform:
        Optional precomputed ``{center, scale}``. When omitted the mesh is
        centred at the origin and scaled so its max extent equals 2 (upstream
        convention); the inverse is applied to the STEP so it returns to
        metric scene units.
    device:
        ``"cuda"`` / ``"cpu"`` / ``None`` (auto).

    Returns
    -------
    dict with keys ``ok``, ``cadquery_src``, ``step_path``, ``error``,
    ``provenance``. On missing torch/weights returns
    ``ok=False, error='not provisioned…'`` without raising.
    """
    base = {
        "ok": False,
        "cadquery_src": None,
        "step_path": None,
        "error": None,
        "provenance": dict(PROVENANCE),
    }

    if not is_provisioned():
        # Still allow a more precise message if only one half is missing.
        if not _torch_importable():
            base["error"] = not_provisioned_error()
            return base
        if weights_path() is None:
            base["error"] = not_provisioned_error()
            return base

    try:
        pre = preprocess_mesh(mesh_path, scale_transform=scale_transform)
    except Exception as exc:  # noqa: BLE001
        base["error"] = f"preprocess failed: {exc}"
        return base

    try:
        src = generate_cadquery(pre["points"], device=device)
    except Exception as exc:  # noqa: BLE001
        base["error"] = f"CAD-Recode generate failed: {exc}"
        return base

    base["cadquery_src"] = src
    mat = materialize_cadquery(
        src,
        session_dir,
        pre["scale_transform"],
        timeout_s=DEFAULT_TIMEOUT_S,
    )
    if not mat.get("ok"):
        base["error"] = mat.get("error") or "materialise failed"
        return base

    base["ok"] = True
    base["step_path"] = mat.get("step_path")
    base["error"] = None
    base["scale_transform"] = pre["scale_transform"]
    return base


def provision_weights(local_dir: Optional[str] = None) -> dict[str, Any]:
    """Download CAD-Recode weights via huggingface_hub (network).

    User-facing helper — not called automatically by the readiness probe.
    """
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"huggingface_hub not installed: {exc}",
        }
    try:
        path = snapshot_download(
            MODEL_ID,
            local_dir=local_dir,
            local_dir_use_symlinks=False if local_dir else None,
        )
        return {"ok": True, "path": path, "model": MODEL_ID}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == _runner_payload_path_arg():
        if len(argv) < 2:
            sys.stderr.write("usage: cad_recode --cad-recode-exec <payload.json>\n")
            return 2
        return _exec_runner(argv[1])
    sys.stderr.write(
        "cad_recode: library module. Use build_parametric() or "
        f"--cad-recode-exec <payload.json>. Weights: {MODEL_ID}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
