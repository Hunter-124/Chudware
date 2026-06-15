# Local AI Runtime

AI-CAD supports three AI-provider shapes:

- `anthropic` / `openai`: cloud providers, blocked when `offline_mode` is true.
- `openai` with a loopback `openai.base_url`: user-managed local OpenAI-compatible server.
- `llamacpp`: managed local llama.cpp server, launched from AppData when available.

## Managed llama.cpp

The managed provider uses llama.cpp's OpenAI-compatible `llama-server`.

Runtime locations:

- Server install root: `%APPDATA%\AI-CAD\llamacpp`
- Model directory: `%APPDATA%\AI-CAD\models`
- Server log: `%APPDATA%\AI-CAD\logs\llamacpp-server.log`

Release payloads may include optional top-level folders:

- `llamacpp\` containing `llama-server.exe`, `server.exe`, `llama-server`, or `server`
- `models\` containing one or more `.gguf` files

`installer.provision` calls `installer.llamacpp.install_from_bundle()` after the Python runtime is
ready. The installer and release builder stage these optional folders from the repo or release root.
No internet download is performed by this path.

## Config Contract

Example:

```json
{
  "provider": "llamacpp",
  "model": "local-gguf",
  "offline_mode": true,
  "llamacpp": {
    "base_url": "http://127.0.0.1:1234/v1",
    "model_path": "%APPDATA%/AI-CAD/models/model.gguf",
    "auto_start": true,
    "ctx_size": 8192,
    "gpu_layers": -1,
    "vision": false
  }
}
```

`model_path` expands `%APPDATA%` and `~`. If the default placeholder `model.gguf` is not present,
the runtime falls back to the first `.gguf` in `%APPDATA%\AI-CAD\models`.

When `offline_mode` is true, a remote `llamacpp.base_url` is refused before the provider probes the
server. Readiness requires a successful OpenAI-compatible `/v1/models` response; a random process
listening on the port is not accepted.

## Verification

Useful local checks:

```powershell
py -3 -m installer.llamacpp --check
py -3 -m agent.test_offline_mode
python scripts\feature_surface_audit.py
python scripts\gui_lint.py --rule icons
python scripts\gui_lint.py --rule theme
```

