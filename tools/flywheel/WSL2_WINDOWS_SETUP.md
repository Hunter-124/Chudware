# Running the Fleet Training Runner on Windows / WSL2 (T-H6)

## OS detection

The runner calls `detect_capability()` which maps `platform.system()` to one of
`linux`, `windows`, or `macos`.

| Environment | `cap["os"]` | `cap["wsl2"]` | Notes |
|---|---|---|---|
| Native Linux | `linux` | `False` | Standard case |
| WSL2 on Windows | `linux` | `True` | `/proc/version` contains "microsoft" |
| Native Windows | `windows` | `False` | No WSL2 |

**Key point:** WSL2 runners report `os: linux`. They ARE running Linux, so they
correctly claim `os: linux` and `os: any` jobs. They will NOT claim `os: windows`
jobs.

## What jobs each runner can claim

| Runner | Claims `os: any` | Claims `os: linux` | Claims `os: windows` |
|---|---|---|---|
| Native Linux | Yes | Yes | No |
| WSL2 on Windows | Yes | Yes | No |
| Native Windows | Yes | No | Yes |

## Setup on Windows (WSL2 path — recommended for training)

1. Install WSL2 with Ubuntu 22.04:
   ```
   wsl --install -d Ubuntu-22.04
   ```

2. Install CUDA toolkit inside WSL2 (follow NVIDIA WSL2 CUDA guide for your driver version).

3. Install Python deps inside WSL2:
   ```bash
   pip install trl peft transformers accelerate mlflow PyYAML
   pip install unsloth   # optional — fast LoRA via custom CUDA kernels
   pip install dvc       # for dataset pulls
   ```

4. Set MLflow tracking URI (the host machine running docker-compose.mlflow.yml):
   ```bash
   export MLFLOW_TRACKING_URI=http://localhost:5000
   # or, if MLflow runs on the Windows host from WSL2:
   export MLFLOW_TRACKING_URI=http://$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):5000
   ```

5. DVC remote — use a path accessible from WSL2:
   - Local NAS: mount via `/mnt/c/...` (Windows drive) or network mount
   - Example plain absolute path (no `file://`): `/mnt/c/dvc-remote`
   - Configure once: `dvc remote add -d origin /mnt/c/dvc-remote`

6. Run the runner from the repo root inside WSL2:
   ```bash
   python -m tools.flywheel.runner --owner myhost-wsl2 run
   ```

## Setup on native Windows (no WSL2)

Native Windows runners can only claim `os: windows` or `os: any` jobs.
TRL/Unsloth on native Windows is experimental; WSL2 is strongly preferred.

1. Install Python 3.11+ via python.org or conda.
2. Install CUDA toolkit for Windows.
3. `pip install trl peft transformers accelerate mlflow PyYAML dvc`
4. Run: `python -m tools.flywheel.runner --owner myhost-win run`

## VRAM and 4-bit quantization

- Unsloth 4-bit quantization reduces VRAM from ~16 GB (full BF16) to ~5-6 GB for Qwen3-4B.
- Minimum for smoke jobs (`smoke-sft-001`): **8 GB VRAM** with 4-bit quant.
- Set `unsloth.load_in_4bit: false` in the recipe to use full precision (requires ~16 GB).

## MLflow UI

1. Start MLflow on the host (or hunter-pc):
   ```bash
   docker compose -f tools/flywheel/docker-compose.mlflow.yml up -d
   ```
2. Open http://localhost:5000 in a browser.
3. If the runner cannot reach the server, it automatically falls back to `./mlruns/`
   (local file store in the current directory).
