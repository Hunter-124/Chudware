"""tools/flywheel/runner.py — Fleet training runner (T-H6).

Claim-based job queue: jobs are markdown files in training/jobs/ with atomic
O_EXCL file-lock claim (mirrors work/tools/work.py). Runner checks capability
(OS, VRAM, CUDA), pulls dataset via DVC, runs training (lazy-imported), pushes
adapter, logs to MLflow, posts a work_note.

Usage:
    python -m tools.flywheel.runner --owner myhost claim     # claim + show job
    python -m tools.flywheel.runner --owner myhost run       # claim + execute
    python -m tools.flywheel.runner --owner myhost run --job smoke-sft-001  # specific

All ML imports are lazy — this module is importable with none of them installed.
MLflow degrades to local file store when server is unreachable.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Front-matter parser (mirrors work/tools/work.py — stdlib only)
# ---------------------------------------------------------------------------

def _parse_job(path: Path) -> dict:
    """Parse YAML-ish front matter from a training job markdown file.

    Front matter is between the first and second ``---`` delimiters.
    Returns a dict; raises ValueError if front matter is missing.
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        raise ValueError(f"{path.name}: missing front matter")
    fm: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{path.name}: bad front-matter line: {line!r}")
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [v.strip() for v in inner.split(",") if v.strip()]
        else:
            fm[key] = val
    fm["_path"] = path
    return fm


def _set_job_field(path: Path, **updates: str) -> None:
    """Update one or more front-matter fields in-place (same pattern as work.py).

    Only scalar string values are supported (matches flat front-matter format).
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", text, re.DOTALL)
    if not m:
        raise ValueError(f"{path.name}: missing front matter for update")

    header, body, footer = m.group(1), m.group(2), m.group(3)
    lines = body.splitlines()
    new_lines = []
    updated = set()
    for line in lines:
        clean = line.split("#", 1)[0].rstrip()
        if ":" in clean:
            key = clean.split(":", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                updated.add(key)
                continue
        new_lines.append(line)

    # Append any keys that were not already present
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}: {val}")

    new_body = "\n".join(new_lines)
    rest = text[m.end():]
    path.write_text(header + new_body + footer + rest, encoding="utf-8")


# ---------------------------------------------------------------------------
# Lock helpers (O_EXCL atomic claim — mirrors work.py pattern)
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT = 30      # seconds to wait for lock
_LOCK_STALE   = 120     # seconds before a lockfile is considered stale


def _lock(jobs_dir: Path) -> Path:
    """Acquire an exclusive advisory lock on jobs_dir/.claim.lock.

    Cross-platform O_EXCL lockfile — the exact idiom of work/tools/work.py
    (fcntl is POSIX-only; fleet runners include native Windows).  A lockfile
    older than _LOCK_STALE seconds is treated as abandoned and removed.

    Returns the lockfile Path — caller must pass it to _unlock() to release.
    Raises TimeoutError if the lock is not acquired within _LOCK_TIMEOUT seconds.
    """
    lock_path = jobs_dir / ".claim.lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue  # holder released between open() and stat() — retry
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Could not acquire claim lock on {lock_path} "
                    f"within {_LOCK_TIMEOUT}s"
                )
            time.sleep(0.05)


def _unlock(lock_path: Path) -> None:
    """Release an O_EXCL lockfile-based lock."""
    Path(lock_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def detect_capability() -> dict:
    """Detect this runner's hardware and OS capabilities.

    Returns:
        {
            "os": "linux" | "windows" | "macos",
            "vram_gb": float,    # 0.0 if no CUDA / torch not available
            "cuda": bool,
            "wsl2": bool,        # True when running Linux inside WSL2
        }

    All torch imports are lazy — function is safe when torch is not installed.
    """
    sys_name = platform.system().lower()
    if sys_name == "linux":
        os_name = "linux"
    elif sys_name == "windows":
        os_name = "windows"
    elif sys_name == "darwin":
        os_name = "macos"
    else:
        os_name = sys_name  # unknown — pass through

    # WSL2 detection: /proc/version contains "microsoft" on WSL2
    wsl2 = False
    if os_name == "linux":
        try:
            proc_version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
            wsl2 = "microsoft" in proc_version.lower()
        except OSError:
            pass

    # CUDA / VRAM — lazy torch import
    cuda = False
    vram_gb = 0.0
    try:
        import torch  # type: ignore[import]
        cuda = torch.cuda.is_available()
        if cuda:
            try:
                props = torch.cuda.get_device_properties(0)
                vram_gb = props.total_memory / (1024 ** 3)
            except Exception:
                vram_gb = 0.0
    except ImportError:
        pass

    return {
        "os": os_name,
        "vram_gb": vram_gb,
        "cuda": cuda,
        "wsl2": wsl2,
    }


# ---------------------------------------------------------------------------
# Claim eligibility check
# ---------------------------------------------------------------------------

def _can_claim(job: dict, cap: dict) -> tuple[bool, str]:
    """Check whether this runner can claim the given job.

    Args:
        job: parsed front-matter dict from _parse_job()
        cap: capability dict from detect_capability()

    Returns:
        (True, "") on success, or (False, reason_string) on failure.
    """
    # OS check
    req_os = job.get("requires_os", "any").lower()
    if req_os not in ("any", "", "none") and req_os != cap["os"]:
        return False, f"os:{req_os} required, runner is {cap['os']}"

    # VRAM check
    try:
        req_vram = float(job.get("requires_vram_gb", 0.0) or 0.0)
    except (TypeError, ValueError):
        req_vram = 0.0
    if cap["vram_gb"] < req_vram:
        return False, (
            f"vram_gb:{req_vram} required, runner has {cap['vram_gb']:.1f}"
        )

    # CUDA check
    req_cuda_raw = str(job.get("requires_cuda", "false")).lower()
    req_cuda = req_cuda_raw in ("true", "1", "yes")
    if req_cuda and not cap["cuda"]:
        return False, "cuda required but not available on this runner"

    return True, ""


# ---------------------------------------------------------------------------
# Claim a job (atomic, git-committed under lock)
# ---------------------------------------------------------------------------

def claim_job(
    jobs_dir: Path,
    owner: str,
    job_id: str | None = None,
) -> tuple[Path, dict] | None:
    """Atomically claim a todo job from jobs_dir.

    Scans *.md files, filters for status==todo + unclaimed owner, checks
    capability, then sets owner + status=doing and git-commits under lock.

    Args:
        jobs_dir: directory containing job .md files
        owner:    name to write into the job's owner field
        job_id:   if given, only try to claim this specific job id

    Returns:
        (path, job_dict) if a job was claimed, or None if no eligible job found.
    """
    cap = detect_capability()

    lock_path = _lock(jobs_dir)
    try:
        candidates = sorted(jobs_dir.glob("*.md"))
        for path in candidates:
            try:
                job = _parse_job(path)
            except (ValueError, OSError):
                continue

            # Skip schema/meta files
            if job.get("status") in ("meta", "done", "review", "doing"):
                continue
            if job.get("status") != "todo":
                continue

            # Skip already-owned jobs (owner must be unassigned / empty / none)
            current_owner = job.get("owner", "unassigned").lower().strip()
            if current_owner not in ("unassigned", "", "none"):
                continue

            # Filter by specific job_id if requested
            if job_id is not None and job.get("id") != job_id:
                continue

            # Capability check
            ok, reason = _can_claim(job, cap)
            if not ok:
                continue

            # Claim it: update file, git add + commit
            _set_job_field(path, owner=owner, status="doing")
            job["owner"] = owner
            job["status"] = "doing"

            # Git commit under lock (best-effort — if git fails, we still return the job)
            repo_root = _find_git_root(jobs_dir)
            if repo_root:
                try:
                    rel_path = path.relative_to(repo_root)
                    subprocess.run(
                        ["git", "add", str(rel_path)],
                        cwd=str(repo_root),
                        capture_output=True,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-m",
                         f"chore(T-H6): claim job {job.get('id')} by {owner}"],
                        cwd=str(repo_root),
                        capture_output=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    warnings.warn(
                        f"runner: git commit after claim failed: {exc.stderr}",
                        stacklevel=2,
                    )

            return path, job

        return None
    finally:
        _unlock(lock_path)


def _find_git_root(start: Path) -> Path | None:
    """Walk up from start to find a .git directory. Returns None if not found."""
    current = start.resolve()
    for _ in range(20):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ---------------------------------------------------------------------------
# DVC pull (thin wrapper)
# ---------------------------------------------------------------------------

def _dvc_pull(repo_path: Path, target: str) -> None:
    """Pull a DVC target using dvc_init helpers."""
    from tools.flywheel.dvc_init import dvc_pull  # type: ignore[import]
    try:
        dvc_pull(repo_path, target)
    except Exception as exc:
        warnings.warn(
            f"runner: dvc pull of {target!r} failed (continuing): {exc}",
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Recipe loader
# ---------------------------------------------------------------------------

def _load_recipe(recipe_path: Path) -> dict:
    """Load and parse a YAML recipe file.

    Args:
        recipe_path: absolute or repo-relative path to a .yaml recipe

    Returns:
        Parsed dict. Raises FileNotFoundError or ImportError on failure.
    """
    if not recipe_path.exists():
        raise FileNotFoundError(f"Recipe not found: {recipe_path}")

    text = recipe_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import]
        return yaml.safe_load(text) or {}
    except ImportError:
        # Graceful degradation (same posture as the ML imports): recipes are
        # deliberately flat two-level YAML, so a minimal parser suffices when
        # PyYAML is not installed (e.g. the mcp-venv).
        return _parse_flat_yaml(text)


def _parse_scalar(raw: str) -> Any:
    """Parse a single flat-YAML scalar: null/bool/int/float/quoted/inline-list."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_scalar(v) for v in inner.split(",")] if inner else []
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    low = raw.lower()
    if low in ("null", "~", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _parse_flat_yaml(text: str) -> dict:
    """Minimal two-level YAML parser for T-H6 recipe files (PyYAML fallback).

    Supports exactly the recipe subset: top-level ``key: value`` and one level
    of nesting (``section:`` followed by two-space-indented ``key: value``),
    scalars (str/int/float/bool/null), quoted strings, and inline lists.
    Comments (#) and blank lines are ignored.
    """
    result: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip() if not _in_quotes_hash(line) else line.rstrip()
        if not stripped.strip():
            continue
        indented = stripped.startswith("  ")
        key, sep, val = stripped.strip().partition(":")
        if not sep:
            continue  # not a mapping line; recipe subset has no bare items
        key = key.strip()
        val = val.strip()
        if indented and section is not None:
            section[key] = _parse_scalar(val)
        elif val == "":
            section = {}
            result[key] = section
        else:
            section = None
            result[key] = _parse_scalar(val)
    return result


def _in_quotes_hash(line: str) -> bool:
    """True if the first '#' in *line* sits inside a quoted string."""
    hash_idx = line.find("#")
    if hash_idx < 0:
        return False
    before = line[:hash_idx]
    return before.count('"') % 2 == 1 or before.count("'") % 2 == 1


# ---------------------------------------------------------------------------
# Training execution
# ---------------------------------------------------------------------------

def _run_training(
    job: dict,
    recipe: dict,
    adapter_out: Path,
    dry_run: bool = False,
    _train_fn: Any = None,
) -> dict:
    """Run training for a job, or delegate to _train_fn if provided.

    Args:
        job:        parsed job dict
        recipe:     parsed recipe dict
        adapter_out: directory where the adapter should be written
        dry_run:    if True, write a placeholder and return without real training
        _train_fn:  optional callable(job, recipe, adapter_out) -> metrics dict.
                    Injected by tests to avoid requiring the ML stack.

    Returns:
        metrics dict, e.g. {"train_loss": 0.42}
    """
    adapter_out.mkdir(parents=True, exist_ok=True)

    if _train_fn is not None:
        return _train_fn(job, recipe, adapter_out)

    if dry_run:
        # Write a placeholder adapter config so provenance checks work
        placeholder = {
            "base_model_name_or_path": job.get("base_model", ""),
            "peft_type": "LORA",
            "dry_run": True,
        }
        (adapter_out / "adapter_config.json").write_text(
            json.dumps(placeholder, indent=2), encoding="utf-8"
        )
        return {"train_loss": 0.0}

    # Real training — lazy imports
    method = recipe.get("method", "sft").lower()

    # Try Unsloth first if enabled in recipe
    unsloth_cfg = recipe.get("unsloth", {})
    use_unsloth = unsloth_cfg.get("enabled", True)

    model = None
    tokenizer = None

    if use_unsloth:
        try:
            import unsloth  # type: ignore[import] # noqa: F401
            from unsloth import FastLanguageModel  # type: ignore[import]
            model, tokenizer = FastLanguageModel.from_pretrained(
                model_name=job.get("base_model", ""),
                max_seq_length=unsloth_cfg.get("max_seq_length", 2048),
                load_in_4bit=unsloth_cfg.get("load_in_4bit", True),
            )
            lora_cfg = recipe.get("lora", {})
            model = FastLanguageModel.get_peft_model(
                model,
                r=lora_cfg.get("r", 16),
                lora_alpha=lora_cfg.get("alpha", 32),
                lora_dropout=lora_cfg.get("dropout", 0.05),
                target_modules=lora_cfg.get("target_modules", ["q_proj", "v_proj"]),
            )
        except ImportError:
            use_unsloth = False
            warnings.warn(
                "runner: unsloth not available, falling back to plain TRL",
                stacklevel=2,
            )

    if model is None:
        # Plain TRL path
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import]
            from peft import get_peft_model, LoraConfig  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "Training requires transformers + peft (or unsloth). "
                "Run: pip install trl peft transformers"
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(job.get("base_model", ""))
        base_model = AutoModelForCausalLM.from_pretrained(job.get("base_model", ""))
        lora_cfg = recipe.get("lora", {})
        peft_config = LoraConfig(
            r=lora_cfg.get("r", 16),
            lora_alpha=lora_cfg.get("alpha", 32),
            lora_dropout=lora_cfg.get("dropout", 0.05),
            target_modules=lora_cfg.get("target_modules", ["q_proj", "v_proj"]),
        )
        model = get_peft_model(base_model, peft_config)

    # Build trainer
    try:
        import trl  # type: ignore[import]  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Training requires trl. Run: pip install trl"
        ) from exc

    train_cfg = recipe.get("training", {})

    metrics: dict[str, float] = {}

    if method == "sft":
        from trl import SFTTrainer, SFTConfig  # type: ignore[import]
        trainer_args = SFTConfig(
            output_dir=str(adapter_out),
            max_steps=train_cfg.get("max_steps", 50),
            per_device_train_batch_size=train_cfg.get("per_device_train_batch_size", 1),
            gradient_accumulation_steps=train_cfg.get("gradient_accumulation_steps", 4),
            learning_rate=float(train_cfg.get("learning_rate", 2e-4)),
            warmup_steps=train_cfg.get("warmup_steps", 5),
            fp16=train_cfg.get("fp16", True),
            logging_steps=train_cfg.get("logging_steps", 10),
            save_strategy="no",
        )
        trainer = SFTTrainer(model=model, tokenizer=tokenizer, args=trainer_args)
        result = trainer.train()
        metrics["train_loss"] = getattr(result, "training_loss", 0.0)

    elif method == "dpo":
        from trl import DPOTrainer, DPOConfig  # type: ignore[import]
        dpo_cfg = recipe.get("dpo", {})
        trainer_args = DPOConfig(
            output_dir=str(adapter_out),
            max_steps=train_cfg.get("max_steps", 50),
            per_device_train_batch_size=train_cfg.get("per_device_train_batch_size", 1),
            gradient_accumulation_steps=train_cfg.get("gradient_accumulation_steps", 4),
            learning_rate=float(train_cfg.get("learning_rate", 5e-5)),
            warmup_steps=train_cfg.get("warmup_steps", 5),
            fp16=train_cfg.get("fp16", True),
            logging_steps=train_cfg.get("logging_steps", 10),
            save_strategy="no",
            beta=float(dpo_cfg.get("beta", 0.1)),
            loss_type=dpo_cfg.get("loss_type", "sigmoid"),
        )
        trainer = DPOTrainer(model=model, tokenizer=tokenizer, args=trainer_args)
        result = trainer.train()
        metrics["train_loss"] = getattr(result, "training_loss", 0.0)

    elif method == "grpo":
        from trl import GRPOTrainer, GRPOConfig  # type: ignore[import]
        trainer_args = GRPOConfig(
            output_dir=str(adapter_out),
            max_steps=train_cfg.get("max_steps", 50),
            per_device_train_batch_size=train_cfg.get("per_device_train_batch_size", 1),
            gradient_accumulation_steps=train_cfg.get("gradient_accumulation_steps", 4),
            learning_rate=float(train_cfg.get("learning_rate", 1e-5)),
            warmup_steps=train_cfg.get("warmup_steps", 5),
            fp16=train_cfg.get("fp16", True),
            logging_steps=train_cfg.get("logging_steps", 10),
            save_strategy="no",
        )
        trainer = GRPOTrainer(model=model, tokenizer=tokenizer, args=trainer_args)
        result = trainer.train()
        metrics["train_loss"] = getattr(result, "training_loss", 0.0)

    else:
        raise ValueError(f"Unknown training method: {method!r}")

    # Save adapter
    model.save_pretrained(str(adapter_out))
    if tokenizer is not None:
        tokenizer.save_pretrained(str(adapter_out))

    return metrics


# ---------------------------------------------------------------------------
# MLflow logging
# ---------------------------------------------------------------------------

def _mlflow_run(
    job: dict,
    recipe: dict,
    adapter_out: Path,
    metrics: dict,
    tracking_uri: str,
) -> str | None:
    """Log a training run to MLflow.

    Falls back to local ./mlruns file store if server is unreachable.
    Returns the MLflow run_id, or None if MLflow is unavailable.
    """
    try:
        import mlflow  # type: ignore[import]
    except ImportError:
        warnings.warn(
            "runner: mlflow not installed — skipping experiment logging. "
            "Run: pip install mlflow  (Apache-2.0)",
            stacklevel=2,
        )
        return None

    def _attempt(uri: str) -> str | None:
        mlflow.set_tracking_uri(uri)
        with mlflow.start_run() as run:
            # Log params
            mlflow.log_param("job_id", job.get("id", ""))
            mlflow.log_param("base_model", job.get("base_model", ""))
            mlflow.log_param("dataset_hash", job.get("dataset_hash", ""))
            mlflow.log_param("recipe", job.get("recipe", ""))
            mlflow.log_param("method", recipe.get("method", ""))
            lora_cfg = recipe.get("lora", {})
            mlflow.log_param("lora_r", lora_cfg.get("r", ""))

            # Log metrics
            for key, val in metrics.items():
                try:
                    mlflow.log_metric(key, float(val))
                except (TypeError, ValueError):
                    pass

            # Log adapter artifact if it exists
            if adapter_out.exists():
                mlflow.log_artifact(str(adapter_out))

            return run.info.run_id

    # Try configured server first, fall back to local mlruns
    try:
        return _attempt(tracking_uri)
    except Exception as exc:
        warnings.warn(
            f"runner: MLflow server at {tracking_uri!r} unreachable ({exc}), "
            "falling back to local ./mlruns",
            stacklevel=2,
        )
        try:
            return _attempt("./mlruns")
        except Exception as exc2:
            warnings.warn(
                f"runner: MLflow local fallback also failed ({exc2}) — "
                "no experiment logging",
                stacklevel=2,
            )
            return None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _build_provenance(
    job: dict,
    recipe: dict,
    adapter_out: Path,
    mlflow_run_id: str | None,
) -> dict:
    """Build and write a JSON provenance record to adapter_out/provenance.json.

    Returns the provenance dict.
    """
    provenance = {
        "adapter_out": str(adapter_out),
        "job_id": job.get("id", ""),
        "dataset_hash": job.get("dataset_hash", ""),
        "base_model": job.get("base_model", ""),
        "recipe": job.get("recipe", ""),
        "method": recipe.get("method", ""),
        "mlflow_run_id": mlflow_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    adapter_out.mkdir(parents=True, exist_ok=True)
    (adapter_out / "provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )
    return provenance


# ---------------------------------------------------------------------------
# High-level run_job orchestrator
# ---------------------------------------------------------------------------

def run_job(
    jobs_dir: Path,
    training_root: Path,
    owner: str,
    job_id: str | None = None,
    mlflow_uri: str = "http://localhost:5000",
    dry_run: bool = False,
    _train_fn: Any = None,
) -> dict:
    """Claim and execute a training job end-to-end.

    Steps:
      1. detect_capability()
      2. claim_job() — atomic O_EXCL claim
      3. dvc pull dataset
      4. load recipe yaml
      5. mkdir adapter_out
      6. _run_training() (or _train_fn if injected)
      7. _mlflow_run() — log params/metrics/artifacts
      8. _build_provenance() — write adapter_out/provenance.json
      9. Set job status → "review"
     10. Return summary dict

    Args:
        jobs_dir:      path to training/jobs/ directory
        training_root: repo root for DVC pulls (usually where .dvc/ lives)
        owner:         agent/machine name for claim ownership
        job_id:        if given, claim this specific job; otherwise first eligible
        mlflow_uri:    MLflow tracking server URI (falls back to ./mlruns)
        dry_run:       skip real training; write placeholder adapter_config.json
        _train_fn:     optional callable injected by tests

    Returns:
        dict with keys: job_id, adapter_out, mlflow_run_id, provenance_path
        Raises RuntimeError if no eligible job is found.
    """
    result = claim_job(jobs_dir, owner, job_id)
    if result is None:
        raise RuntimeError(
            f"No eligible job found in {jobs_dir} for owner={owner!r}"
            + (f", job_id={job_id!r}" if job_id else "")
        )

    path, job = result

    # DVC pull dataset (best-effort — offline/smoke runs may not have DVC configured)
    dataset_hash = job.get("dataset_hash", "")
    if dataset_hash:
        _dvc_pull(training_root, dataset_hash)

    # Load recipe
    recipe_path = Path(job.get("recipe", ""))
    if not recipe_path.is_absolute():
        recipe_path = training_root / recipe_path
    recipe = _load_recipe(recipe_path)

    # Prepare adapter output dir
    adapter_out_str = job.get("adapter_out", "")
    adapter_out = Path(adapter_out_str)
    if not adapter_out.is_absolute():
        adapter_out = training_root / adapter_out
    adapter_out.mkdir(parents=True, exist_ok=True)

    # Run training
    metrics = _run_training(
        job, recipe, adapter_out,
        dry_run=dry_run,
        _train_fn=_train_fn,
    )

    # MLflow logging
    mlflow_run_id = _mlflow_run(job, recipe, adapter_out, metrics, mlflow_uri)

    # Provenance record
    provenance = _build_provenance(job, recipe, adapter_out, mlflow_run_id)

    # Mark job as review
    _set_job_field(path, status="review")

    # Git commit status update (best-effort)
    repo_root = _find_git_root(jobs_dir)
    if repo_root:
        try:
            rel_path = path.relative_to(repo_root)
            subprocess.run(
                ["git", "add", str(rel_path)],
                cwd=str(repo_root),
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "commit", "-m",
                 f"chore(T-H6): job {job.get('id')} → review by {owner}"],
                cwd=str(repo_root),
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    return {
        "job_id": job.get("id", ""),
        "adapter_out": str(adapter_out),
        "mlflow_run_id": mlflow_run_id,
        "provenance_path": str(adapter_out / "provenance.json"),
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fleet training runner (T-H6)"
    )
    parser.add_argument("--owner", required=True, help="Runner/machine name")
    parser.add_argument(
        "--jobs-dir", type=Path, default=Path("training/jobs"),
        help="Path to training jobs directory (default: training/jobs)"
    )
    parser.add_argument(
        "--training-root", type=Path, default=Path("training"),
        help="Training repo root for DVC (default: training/)"
    )
    parser.add_argument(
        "--mlflow-uri", default="http://localhost:5000",
        help="MLflow tracking URI (default: http://localhost:5000)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip real training; write placeholder adapter only"
    )

    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # claim: show next claimable job without executing training
    claim_p = subparsers.add_parser("claim", help="Claim next eligible job and print it")
    claim_p.add_argument("--job", default=None, help="Specific job id to claim")

    # run: claim + execute
    run_p = subparsers.add_parser("run", help="Claim and execute a training job")
    run_p.add_argument("--job", default=None, help="Specific job id to run")

    # capability: print this runner's detected capability
    subparsers.add_parser("capability", help="Print detected runner capabilities")

    args = parser.parse_args()

    if args.cmd == "capability":
        cap = detect_capability()
        print(json.dumps(cap, indent=2))
        return

    if args.cmd == "claim":
        result = claim_job(args.jobs_dir, args.owner, getattr(args, "job", None))
        if result is None:
            print("No eligible job found.")
            sys.exit(1)
        path, job = result
        print(f"Claimed: {job.get('id')} ({path})")
        print(json.dumps({k: v for k, v in job.items() if not k.startswith("_")}, indent=2))
        return

    if args.cmd == "run":
        try:
            summary = run_job(
                jobs_dir=args.jobs_dir,
                training_root=args.training_root,
                owner=args.owner,
                job_id=getattr(args, "job", None),
                mlflow_uri=args.mlflow_uri,
                dry_run=args.dry_run,
            )
            print(json.dumps(summary, indent=2))
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
