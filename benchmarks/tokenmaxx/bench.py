#!/usr/bin/env python3
"""Local tokenmaxx bench (T-W29).

Answers Hunter's MAIN-GOAL question: what is the best way to tokenmaxx with local
models on this machine (RTX 3080 Ti, 12 GB) --- diffusion Gemma's high single-stream
TPS vs Gemma 4 E4B / qwen3-4b parallel slots under llama.cpp?

This script drives a *CUDA-enabled* llama-server (built at the same commit as the
bundled CPU-only b9860 --- see report) and measures, per model:

  * single-stream decode TPS + time-to-first-token (TTFT)
  * N-parallel aggregate decode TPS (N in {1,2,4,8} where VRAM allows)
  * VRAM footprint at load and under N-parallel load
  * 24k-context behaviour (load at --ctx-size 24576, long-prompt decode)
  * structured-output adherence:
      - GBNF-constrained (llama.cpp `grammar` field) success rate
      - prompted-JSON + validate_handoff() retry rate (the no-grammar path a
        diffusion backend would be stuck with)
  * three harness-shaped micro-evals (feature_modeler geometry expansion,
    reviewer/council critique, worker tool-call turn) timed end-to-end INCLUDING
    the validate-and-retry tax -> "effective TPS after retry tax".

Design notes
------------
* Re-runnable + machine-readable: writes results/<model>.json and a combined
  results/matrix.json. A human report lives in REPORT.md (hand-written from these
  numbers).
* Uses the REAL structured-output code paths: services.agent.roles.schema_to_gbnf
  (the grammar the worker roles actually emit) and roles.validate_handoff /
  load_role (the real validator). No re-implemented schema logic.
* GPU is shared with the user's desktop. We check free VRAM before each load and
  refuse to load if free < model_estimate + HEADROOM_MB. We unload (kill server)
  between models.

Usage
-----
    # point at a CUDA llama-server binary (built separately; bundled one is CPU-only)
    export TOKENMAXX_LLAMA_SERVER=~/.local/share/AI-CAD/llamacpp/build-cuda/bin/llama-server
    python3 benchmarks/tokenmaxx/bench.py --models qwen3-4b gemma3-4b-qat gemma4-e4b
    python3 benchmarks/tokenmaxx/bench.py --quick        # 1 model, N in {1,2}, smoke
    python3 benchmarks/tokenmaxx/bench.py --list          # show known models + presence

Env knobs
---------
    TOKENMAXX_LLAMA_SERVER   path to a llama-server binary (default: bundled CPU one,
                             which will run but pin every metric to CPU speed).
    TOKENMAXX_HEADROOM_MB    VRAM headroom to leave for the desktop (default 2048).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RESULTS = HERE / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

# Import the REAL structured-output code paths.
sys.path.insert(0, str(REPO))
try:
    from services.agent import roles as _roles  # noqa: E402
except Exception as exc:  # pragma: no cover - import guard
    _roles = None
    _ROLES_IMPORT_ERR = str(exc)
else:
    _ROLES_IMPORT_ERR = ""

# --------------------------------------------------------------------------- config

DEFAULT_LLAMA_SERVER = os.environ.get(
    "TOKENMAXX_LLAMA_SERVER",
    str(Path.home() / ".local/share/AI-CAD/llamacpp/build/bin/llama-server"),
)
HEADROOM_MB = int(os.environ.get("TOKENMAXX_HEADROOM_MB", "2048"))
MODELS_DIR = Path.home() / ".local/share/AI-CAD/models"

# Registry of GGUFs this bench knows how to load. Diffusion Gemma is listed but
# marked not_runnable_here with the reason (see report / provision()).
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "qwen3-4b": {
        "path": MODELS_DIR / "qwen3-4b" / "Qwen3-4B-Q4_K_M.gguf",
        "est_vram_mb": 3200,
        "license": "Apache-2.0",
        "note": "baseline; tool-calling primary",
    },
    "gemma3-4b-qat": {
        "path": MODELS_DIR / "gemma3-4b-qat" / "google_gemma-3-4b-it-qat-Q4_K_M.gguf",
        "est_vram_mb": 3400,
        "license": "Gemma-ToU",
        "note": "QAT alt",
    },
    "gemma4-e4b": {
        "path": MODELS_DIR / "gemma4-e4b" / "gemma-4-E4B-it-Q4_K_M.gguf",
        "est_vram_mb": 5200,
        "license": "Gemma-ToU",
        "note": "Gemma 4 E-series (successor to '3n E4B'); MatFormer edge model",
    },
    "diffusiongemma-26b-a4b": {
        "path": None,
        "est_vram_mb": 16000,
        "license": "Gemma-ToU",
        "note": "NOT runnable on 12GB: 26B-A4B MoE, min Q4 quant = 16GB, needs "
        "llama-diffusion-cli PR#24423 (no server/grammar/parallel). NO-GO.",
        "not_runnable_here": True,
    },
}

# --------------------------------------------------------------------------- vram


def free_vram_mb() -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


def used_vram_mb() -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


# --------------------------------------------------------------------------- server


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """Managed llama-server lifecycle for one model load."""

    def __init__(self, model_path: Path, *, n_ctx: int, n_parallel: int,
                 server_bin: str):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_parallel = n_parallel
        self.server_bin = server_bin
        self.port = _pick_free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: Optional[subprocess.Popen] = None
        self.gpu_offloaded: Optional[bool] = None
        self.log_path = RESULTS / f"_server_{self.port}.log"

    def start(self, timeout: float = 180.0) -> None:
        cmd = [
            self.server_bin,
            "-m", str(self.model_path),
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--n-gpu-layers", "99",
            "--ctx-size", str(self.n_ctx),
            "--parallel", str(self.n_parallel),
            "--no-webui",
        ]
        logf = self.log_path.open("w")
        # Some llama.cpp builds ship a thin launcher that dlopen's
        # libllama-server-impl.so next to the binary. Ensure that dir is on the
        # library path so the CUDA build loads.
        env = dict(os.environ)
        bindir = str(Path(self.server_bin).resolve().parent)
        env["LD_LIBRARY_PATH"] = bindir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                     env=env)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited early rc={self.proc.returncode}; "
                    f"see {self.log_path}"
                )
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                    if b'"status":"ok"' in r.read():
                        break
            except Exception:
                time.sleep(1.0)
        else:
            self.stop()
            raise RuntimeError(f"server did not become healthy in {timeout}s")
        # Detect silent CPU fallback (CPU-only build ignores --n-gpu-layers).
        log_text = self.log_path.read_text(errors="ignore")
        self.gpu_offloaded = "no usable GPU found" not in log_text

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.proc = None

    # --- generation -------------------------------------------------------

    def chat(self, messages: list[dict], *, max_tokens: int = 128,
             temperature: float = 0.0, grammar: Optional[str] = None,
             stream_ttft: bool = False) -> dict:
        """One /v1/chat/completions call. Returns timing + text + usage."""
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "cache_prompt": False,
        }
        if grammar:
            body["grammar"] = grammar
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + "/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"},
        )
        if stream_ttft:
            body["stream"] = True
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                self.base + "/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json"},
            )
            t0 = time.perf_counter()
            ttft = None
            chunks: list[str] = []
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        j = json.loads(payload)
                        delta = j["choices"][0]["delta"].get("content", "")
                    except Exception:
                        delta = ""
                    if delta is None:
                        delta = ""
                    if delta and ttft is None:
                        ttft = time.perf_counter() - t0
                    chunks.append(delta)
            elapsed = time.perf_counter() - t0
            text = "".join(chunks)
            return {"text": text, "ttft_s": ttft, "elapsed_s": elapsed,
                    "completion_tokens": None}
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as resp:
            j = json.loads(resp.read())
        elapsed = time.perf_counter() - t0
        usage = j.get("usage", {})
        text = j["choices"][0]["message"]["content"]
        # llama-server reports an authoritative `timings` block: decode rate
        # (predicted_per_second) excludes HTTP/queue/prompt-eval noise. Prefer it
        # for single-stream decode TPS; fall back to client-side wall time.
        timings = j.get("timings", {}) or {}
        return {
            "text": text,
            "elapsed_s": elapsed,
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "server_decode_tps": timings.get("predicted_per_second"),
            "server_predicted_ms": timings.get("predicted_ms"),
            "server_prompt_ms": timings.get("prompt_ms"),
        }

    def warmup(self) -> None:
        """One throwaway generation to pay CUDA-graph capture before timing."""
        try:
            self.chat([{"role": "user", "content": "hi"}], max_tokens=4)
        except Exception:
            pass


# --------------------------------------------------------------------------- prompts

# Real handoff schemas pulled from the role cards (fallback inline if roles import
# fails, so the bench still runs standalone).
_FEATURE_MODELER_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "outputs"],
    "properties": {
        "status": {"enum": ["done", "blocked", "escalated"]},
        "summary": {"type": "string", "maxLength": 600},
        "outputs": {
            "type": "object",
            "required": ["doc_id", "nodes_completed", "checks_run",
                         "reasoning_summary"],
            "properties": {
                "doc_id": {"type": "string"},
                "nodes_completed": {"type": "array", "items": {"type": "string"}},
                "checks_run": {"type": "array", "items": {
                    "type": "object", "required": ["check", "result"]}},
                "reasoning_summary": {"type": "string", "maxLength": 800},
            },
        },
    },
}


def _load_schema(role_id: str, fallback: dict) -> dict:
    if _roles is not None:
        try:
            return _roles.load_role(role_id).handoff_schema
        except Exception:
            pass
    return fallback


def _gbnf_for(schema: dict) -> Optional[str]:
    if _roles is not None:
        try:
            return _roles.schema_to_gbnf(schema)
        except Exception:
            return None
    return None


def _validate(role_id: str, schema: dict, envelope: dict) -> list[str]:
    """Validate an envelope against the schema using the real validator when
    possible (needs a RoleCard); else the inline subset validator."""
    if _roles is not None:
        try:
            card = _roles.load_role(role_id)
            # validate_handoff also checks envelope.role == card.id; the model
            # won't emit `role`, so inject it to isolate the *structure* check.
            env = dict(envelope)
            env.setdefault("role", card.id)
            return _roles.validate_handoff(card, env)
        except Exception:
            pass
    # Fallback: structural subset.
    errs: list[str] = []
    _subset_validate(envelope, schema, "handoff", errs)
    return errs


def _subset_validate(value, schema, path, errs):
    stype = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if stype == "object" and isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing {req!r}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                _subset_validate(value[k], sub, f"{path}.{k}", errs)
    elif stype == "array" and isinstance(value, list):
        it = schema.get("items")
        if it:
            for i, item in enumerate(value):
                _subset_validate(item, it, f"{path}[{i}]", errs)


# Micro-eval prompts: (a) worker geometry expansion, (b) council critique,
# (c) worker tool-call turn. Each targets a real handoff schema.

def _feature_modeler_prompt() -> tuple[list[dict], dict, str]:
    schema = _load_schema("feature_modeler", _FEATURE_MODELER_SCHEMA)
    sys_p = (
        "You are the FEATURE MODELER. You just completed a build-graph node that "
        "extrudes the base plate (100x60x8mm) and drilled a bolt_circle of 4 M5 "
        "clearance holes. Emit ONLY a JSON object matching this schema, no prose:\n"
        + json.dumps(schema)
    )
    user_p = ("Report the handoff for the two completed nodes n_base_extrude and "
              "n_bolt_circle. checks_run should include a describe_part and a "
              "check_feature_overlap result.")
    return ([{"role": "system", "content": sys_p},
             {"role": "user", "content": user_p}], schema, "feature_modeler")


def _reviewer_prompt() -> tuple[list[dict], dict, str]:
    schema = _load_schema("reviewer", {
        "type": "object", "required": ["status", "summary", "outputs"],
        "properties": {
            "status": {"enum": ["done", "blocked", "escalated"]},
            "summary": {"type": "string", "maxLength": 600},
            "outputs": {"type": "object", "required": [
                "criterion_results", "battery_verdict", "not_checked",
                "reasoning_summary"], "properties": {}},
        }})
    sys_p = (
        "You are the REVIEWER, the pipeline's independent verifier. Critique the "
        "part against its acceptance criteria and emit ONLY a JSON object matching "
        "this schema, no prose:\n" + json.dumps(schema))
    user_p = ("Part: bracket, 2 criteria (wall_thickness>=3mm PASS via wall_thickness "
              "tool; mass<=250g FAIL measured 310g). One item not checked: fatigue "
              "(no load spec). Emit the reviewer handoff.")
    return ([{"role": "system", "content": sys_p},
             {"role": "user", "content": user_p}], schema, "reviewer")


def _worker_toolcall_prompt() -> tuple[list[dict], dict, str]:
    # A worker tool-call turn: emit a single tool call as JSON.
    schema = {
        "type": "object",
        "required": ["tool", "args"],
        "properties": {
            "tool": {"enum": ["add_features", "describe_part", "measure_part",
                              "check_feature_overlap", "part_search"]},
            "args": {"type": "object"},
        },
    }
    sys_p = ("You are a FEATURE MODELER mid-node. Emit ONLY a JSON tool call "
             "matching this schema, no prose:\n" + json.dumps(schema))
    user_p = ("You need to verify the top face is planar after the last cut. "
              "Emit the single tool call to inspect the part.")
    return ([{"role": "system", "content": sys_p},
             {"role": "user", "content": user_p}], schema, "_toolcall")


MICRO_EVALS = {
    "feature_modeler_expansion": _feature_modeler_prompt,
    "reviewer_critique": _reviewer_prompt,
    "worker_toolcall": _worker_toolcall_prompt,
}


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort: pull the first balanced {...} object out of model text."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


# --------------------------------------------------------------------------- measurements


def measure_single_stream(srv: Server, *, max_tokens: int = 256) -> dict:
    """Single-stream decode TPS + TTFT on a fixed prompt."""
    msgs = [{"role": "user",
             "content": "Write a detailed paragraph about CNC milling of aluminum "
                        "brackets, covering feeds, speeds, and workholding."}]
    # TTFT via streaming.
    s = srv.chat(msgs, max_tokens=max_tokens, stream_ttft=True)
    # TPS via non-streaming. Prefer the SERVER's authoritative decode rate
    # (excludes HTTP/queue/prompt-eval); client wall-time is reported alongside
    # for cross-checking.
    n = srv.chat(msgs, max_tokens=max_tokens)
    client_tps = None
    if n["completion_tokens"] and n["elapsed_s"] > 0:
        client_tps = n["completion_tokens"] / n["elapsed_s"]
    server_tps = n.get("server_decode_tps")
    return {
        "ttft_s": round(s["ttft_s"], 4) if s.get("ttft_s") else None,
        # decode_tps = the authoritative server number when available.
        "decode_tps": round(server_tps, 2) if server_tps else (
            round(client_tps, 2) if client_tps else None),
        "server_decode_tps": round(server_tps, 2) if server_tps else None,
        "client_wall_tps": round(client_tps, 2) if client_tps else None,
        "completion_tokens": n["completion_tokens"],
        "elapsed_s": round(n["elapsed_s"], 3),
    }


def measure_parallel(srv: Server, n_slots: int, *, max_tokens: int = 256) -> dict:
    """N concurrent requests; aggregate TPS = sum(tokens)/wall_time."""
    msgs_variants = [
        [{"role": "user", "content": f"Explain machining topic #{i}: describe a "
          "different aspect of CNC aluminum bracket production in one paragraph."}]
        for i in range(n_slots)
    ]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_slots) as ex:
        futs = [ex.submit(srv.chat, m, max_tokens=max_tokens) for m in msgs_variants]
        results = [f.result() for f in futs]
    wall = time.perf_counter() - t0
    total_tokens = sum(r["completion_tokens"] or 0 for r in results)
    peak_used = used_vram_mb()
    # Mean per-stream server decode TPS (for the single-stream self-check at N=1).
    per_stream = [r.get("server_decode_tps") for r in results
                  if r.get("server_decode_tps")]
    mean_server_tps = (round(sum(per_stream) / len(per_stream), 2)
                       if per_stream else None)
    return {
        "n_slots": n_slots,
        "wall_s": round(wall, 3),
        "total_completion_tokens": total_tokens,
        "aggregate_tps": round(total_tokens / wall, 2) if wall > 0 else None,
        "mean_per_stream_server_tps": mean_server_tps,
        "vram_used_mb": peak_used,
    }


def measure_structured(srv: Server, *, trials: int = 8) -> dict:
    """GBNF-constrained vs prompted-JSON+retry adherence on the feature_modeler
    handoff schema. Reports success rate and mean retries for the JSON path."""
    msgs, schema, role_id = _feature_modeler_prompt()
    gbnf = _gbnf_for(schema)

    # --- GBNF path ---
    gbnf_ok = 0
    gbnf_supported = gbnf is not None
    gbnf_times = []
    if gbnf_supported:
        for _ in range(trials):
            try:
                r = srv.chat(msgs, max_tokens=512, grammar=gbnf)
                obj = _extract_json(r["text"])
                if obj is not None and not _validate(role_id, schema, obj):
                    gbnf_ok += 1
                gbnf_times.append(r["elapsed_s"])
            except urllib.error.HTTPError:
                gbnf_supported = False
                break
            except Exception:
                pass

    # --- prompted JSON + validator retry path (the diffusion-style fallback) ---
    MAX_RETRIES = 3
    json_first_ok = 0
    json_eventual_ok = 0
    retry_counts = []
    json_times = []
    for _ in range(trials):
        conv = list(msgs)
        ok = False
        used = 0
        t_acc = 0.0
        for attempt in range(MAX_RETRIES + 1):
            r = srv.chat(conv, max_tokens=512)
            t_acc += r["elapsed_s"]
            obj = _extract_json(r["text"])
            errs = _validate(role_id, schema, obj) if obj is not None else ["no json"]
            if not errs:
                ok = True
                if attempt == 0:
                    json_first_ok += 1
                break
            used = attempt + 1
            conv = list(msgs) + [
                {"role": "assistant", "content": r["text"][:1500]},
                {"role": "user", "content":
                    "That was invalid: " + "; ".join(errs[:4]) +
                    ". Emit ONLY the corrected JSON object, nothing else."},
            ]
        if ok:
            json_eventual_ok += 1
        retry_counts.append(used)
        json_times.append(t_acc)

    return {
        "trials": trials,
        "gbnf_supported": gbnf_supported,
        "gbnf_success_rate": round(gbnf_ok / trials, 3) if gbnf_supported else None,
        "gbnf_mean_s": round(sum(gbnf_times) / len(gbnf_times), 3) if gbnf_times else None,
        "json_first_pass_rate": round(json_first_ok / trials, 3),
        "json_eventual_rate": round(json_eventual_ok / trials, 3),
        "json_mean_retries": round(sum(retry_counts) / len(retry_counts), 2),
        "json_mean_s_with_retries": round(sum(json_times) / len(json_times), 3),
    }


def measure_micro_evals(srv: Server) -> dict:
    """Harness-shaped turns timed end-to-end INCLUDING validate/retry.
    Reports effective TPS after the retry tax (JSON path) and GBNF one-shot."""
    out = {}
    for name, builder in MICRO_EVALS.items():
        msgs, schema, role_id = builder()
        gbnf = _gbnf_for(schema)
        # GBNF one-shot (if supported).
        gbnf_res = None
        if gbnf:
            try:
                r = srv.chat(msgs, max_tokens=512, grammar=gbnf)
                obj = _extract_json(r["text"])
                valid = obj is not None and not _validate(role_id, schema, obj)
                tps = (r["completion_tokens"] / r["elapsed_s"]
                       if r["completion_tokens"] and r["elapsed_s"] > 0 else None)
                gbnf_res = {"valid": bool(valid), "elapsed_s": round(r["elapsed_s"], 3),
                            "tps": round(tps, 2) if tps else None}
            except Exception as e:
                gbnf_res = {"error": str(e)[:120]}
        # JSON+retry end-to-end.
        conv = list(msgs)
        t0 = time.perf_counter()
        toks = 0
        retries = 0
        valid = False
        for attempt in range(4):
            r = srv.chat(conv, max_tokens=512)
            toks += r["completion_tokens"] or 0
            obj = _extract_json(r["text"])
            errs = _validate(role_id, schema, obj) if obj is not None else ["no json"]
            if not errs:
                valid = True
                break
            retries += 1
            conv = list(msgs) + [
                {"role": "assistant", "content": r["text"][:1500]},
                {"role": "user", "content": "Invalid: " + "; ".join(errs[:3]) +
                 ". Emit ONLY corrected JSON."}]
        wall = time.perf_counter() - t0
        out[name] = {
            "gbnf": gbnf_res,
            "json_retry": {
                "valid": valid, "retries": retries,
                "wall_s": round(wall, 3), "tokens": toks,
                "effective_tps": round(toks / wall, 2) if wall > 0 else None,
            },
        }
    return out


def measure_long_context(srv: Server, *, target_ctx: int = 24576) -> dict:
    """Load a large prompt near target_ctx and decode; report TTFT + decode TPS,
    proving the model handles ~24k context without failure."""
    # ~4 chars/token; build ~ (target_ctx - 1024) tokens of filler.
    filler_tokens = max(2000, target_ctx - 1500)
    filler = ("The following is machining documentation. " * (filler_tokens // 6))
    msgs = [{"role": "user",
             "content": filler + "\n\nSummarize the above in exactly one sentence."}]
    try:
        r = srv.chat(msgs, max_tokens=64, stream_ttft=True)
        n = srv.chat(msgs, max_tokens=64)
        server_tps = n.get("server_decode_tps")
        client_tps = (n["completion_tokens"] / n["elapsed_s"]
                      if n["completion_tokens"] and n["elapsed_s"] > 0 else None)
        return {
            "ok": True,
            "prompt_tokens": n.get("prompt_tokens"),
            "ttft_s": round(r["ttft_s"], 3) if r.get("ttft_s") else None,
            "decode_tps": round(server_tps, 2) if server_tps else (
                round(client_tps, 2) if client_tps else None),
            "prompt_eval_ms": n.get("server_prompt_ms"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


# --------------------------------------------------------------------------- driver


def bench_model(model_id: str, *, server_bin: str, parallel_ns: list[int],
                trials: int, do_long_ctx: bool) -> dict:
    entry = MODEL_REGISTRY[model_id]
    result: dict[str, Any] = {
        "model_id": model_id,
        "license": entry["license"],
        "note": entry["note"],
        "server_bin": server_bin,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if entry.get("not_runnable_here"):
        result["status"] = "NOT_RUNNABLE_HERE"
        result["reason"] = entry["note"]
        return result
    path = entry["path"]
    if not path or not path.exists():
        result["status"] = "MODEL_MISSING"
        result["reason"] = f"GGUF not found at {path}"
        return result

    free = free_vram_mb()
    result["free_vram_mb_before"] = free
    if free is not None and free < entry["est_vram_mb"] + HEADROOM_MB:
        result["status"] = "SKIP_LOW_VRAM"
        result["reason"] = (f"free {free}MB < est {entry['est_vram_mb']}MB + "
                            f"headroom {HEADROOM_MB}MB")
        return result

    max_parallel = max(parallel_ns)
    # single-stream + structured + micro first (n_parallel=1 server), then a
    # second load for parallel with n_parallel=max.
    base_used = used_vram_mb() or 0

    # Phase 1: n_parallel=1 for latency-bound metrics.
    srv = Server(path, n_ctx=8192, n_parallel=1, server_bin=server_bin)
    try:
        srv.start()
        srv.warmup()  # pay CUDA-graph capture before timing
        result["gpu_offloaded"] = srv.gpu_offloaded
        loaded_used = used_vram_mb()
        result["vram_footprint_mb"] = (loaded_used - base_used) if loaded_used else None
        result["vram_used_total_mb"] = loaded_used
        result["single_stream"] = measure_single_stream(srv)
        result["structured"] = measure_structured(srv, trials=trials)
        result["micro_evals"] = measure_micro_evals(srv)
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)[:300]
        srv.stop()
        return result
    finally:
        srv.stop()
    time.sleep(2)

    # Phase 2: parallel slots.
    parallel_results = []
    srv2 = Server(path, n_ctx=max(8192, 2048 * max_parallel),
                  n_parallel=max_parallel, server_bin=server_bin)
    try:
        srv2.start()
        srv2.warmup()
        for n in parallel_ns:
            free_now = free_vram_mb()
            if free_now is not None and free_now < HEADROOM_MB:
                parallel_results.append({"n_slots": n, "status": "SKIP_LOW_VRAM",
                                         "free_vram_mb": free_now})
                continue
            parallel_results.append(measure_parallel(srv2, n))
    except Exception as e:
        parallel_results.append({"status": "ERROR", "error": str(e)[:200]})
    finally:
        srv2.stop()
    result["parallel"] = parallel_results

    # Self-check (advisor): single-stream decode TPS must agree with the
    # per-stream server TPS at N=1 (same physical operation). A large mismatch
    # means the harness is measuring queue/HTTP noise, not decode speed.
    try:
        ss = (result.get("single_stream") or {}).get("decode_tps")
        n1 = next((p.get("mean_per_stream_server_tps") for p in parallel_results
                   if isinstance(p, dict) and p.get("n_slots") == 1), None)
        if ss and n1 and n1 > 0:
            ratio = ss / n1
            result["selfcheck_ss_vs_n1"] = {
                "single_stream_tps": ss, "n1_per_stream_tps": n1,
                "ratio": round(ratio, 2),
                "ok": 0.8 <= ratio <= 1.25,
            }
    except Exception:
        pass
    time.sleep(2)

    # Phase 3: 24k context (separate load with big ctx).
    if do_long_ctx:
        srv3 = Server(path, n_ctx=24576, n_parallel=1, server_bin=server_bin)
        try:
            srv3.start()
            srv3.warmup()
            result["long_context_24k"] = measure_long_context(srv3)
            lc_used = used_vram_mb()
            result["vram_used_24k_mb"] = lc_used
        except Exception as e:
            result["long_context_24k"] = {"ok": False, "error": str(e)[:200]}
        finally:
            srv3.stop()

    result["status"] = "OK"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="*",
                    default=["qwen3-4b", "gemma3-4b-qat", "gemma4-e4b"])
    ap.add_argument("--server", default=DEFAULT_LLAMA_SERVER)
    ap.add_argument("--parallel", nargs="*", type=int, default=[1, 2, 4, 8])
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--no-long-ctx", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="1 model, N in {1,2}, trials=3, no long ctx")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for mid, e in MODEL_REGISTRY.items():
            present = "n/a" if e.get("not_runnable_here") else (
                "present" if (e["path"] and e["path"].exists()) else "MISSING")
            print(f"  {mid:26s} {present:8s} {e['license']:12s} {e['note']}")
        print(f"\nllama-server: {args.server}")
        print(f"roles import: {'ok' if _roles else 'FAILED: ' + _ROLES_IMPORT_ERR}")
        fv = free_vram_mb()
        print(f"free VRAM: {fv} MB" if fv else "free VRAM: (nvidia-smi unavailable)")
        return 0

    if args.quick:
        args.models = args.models[:1]
        args.parallel = [1, 2]
        args.trials = 3
        args.no_long_ctx = True

    if not Path(args.server).exists():
        print(f"WARNING: llama-server not found at {args.server}", file=sys.stderr)
        print("Set TOKENMAXX_LLAMA_SERVER to a CUDA-enabled build.", file=sys.stderr)

    print(f"# tokenmaxx bench | server={args.server}")
    print(f"# roles code paths: {'REAL' if _roles else 'FALLBACK (' + _ROLES_IMPORT_ERR + ')'}")
    print(f"# free VRAM: {free_vram_mb()} MB | headroom {HEADROOM_MB} MB\n")

    matrix = {
        "meta": {
            "server_bin": args.server,
            "roles_real": _roles is not None,
            "headroom_mb": HEADROOM_MB,
            "parallel_ns": args.parallel,
            "trials": args.trials,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "gpu": subprocess.getoutput(
                "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"),
        },
        "models": {},
    }
    for mid in args.models:
        if mid not in MODEL_REGISTRY:
            print(f"!! unknown model {mid}", file=sys.stderr)
            continue
        print(f"=== {mid} ===", flush=True)
        res = bench_model(mid, server_bin=args.server, parallel_ns=args.parallel,
                          trials=args.trials, do_long_ctx=not args.no_long_ctx)
        matrix["models"][mid] = res
        (RESULTS / f"{mid}.json").write_text(json.dumps(res, indent=2))
        print(f"    status={res.get('status')} "
              f"gpu_offloaded={res.get('gpu_offloaded')} "
              f"ss_tps={res.get('single_stream', {}).get('decode_tps')}", flush=True)

    (RESULTS / "matrix.json").write_text(json.dumps(matrix, indent=2))
    print(f"\nWrote {RESULTS / 'matrix.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
