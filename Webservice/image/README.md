# chudware-session image

Per-user streamed Chudware CAD seat for chudware-web.

- **Base:** `registry.fedoraproject.org/fedora:44` (glibc match with host build)
- **Tag:** `localhost/chudware-session:latest`
- **Stream port:** `8080` (HTTP + WS)
- **Default backend:** `STREAM_BACKEND=novnc` (x11vnc + websockify + noVNC)
- **Optional:** `STREAM_BACKEND=selkies` if `selkies` pip install succeeded at build

## Why Fedora (not Selkies Ubuntu GHCR)

Host Chudware is built against **glibc 2.43** + system **OCCT 7.9** on Fedora 44.
`ghcr.io/selkies-project/nvidia-glx-desktop:24.04` is Ubuntu 24.04 (glibc 2.39) and
was **rejected** as a base (advisor Q4 / DESIGN.md).

## CEF note (advisor §A6)

There is **no runtime flag** to disable the vendor browser. `CHUDWARE_ENABLE_CEF` is
compile-time only; the linked `Chudware` binary `NEEDED`s `libcef.so` and
`CefInitialize` aborts without `icudtl.dat` / `*.pak` next to the exe. The image
therefore ships a **stripped** `libcef.so` (~417 MiB) + minimal CEF runtime.
`CHUDWARE_CEF_LAZY=1` defers cold init until a web surface opens.

## Files

| Path | Role |
|------|------|
| `stage.sh` | Assemble `stage/` from host build + CEF + **Python app tree** |
| `stage/` | **Build output only** — do not commit (gitignore) |
| `Containerfile` | Fedora 44 image + `/opt/chudware/runtime/venv` |
| `entrypoint.sh` | Xvfb → **PLM clone/pull** → Chudware → novnc/selkies; exports CHUDWARE_*; SIGTERM cleanup |

## Python AI worker

The in-app AI agent needs a Python worker process. The image ships:

| Path | Role |
|------|------|
| `/opt/chudware/app/` | Staged packages: `engine`, `agent`, `mcp_server`, `installer`, `services/*` |
| `/opt/chudware/runtime/venv/` | pip env from `requirements.txt` (**not** under HOME) |
| `CHUDWARE_PYTHON` | `/opt/chudware/runtime/venv/bin/python` |
| `CHUDWARE_REPO` | `/opt/chudware/app` |
| `CHUDWARE_PROMPTS_DIR` | `/opt/chudware/app/agent/prompts` |

Provider API keys (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, …) pass through the
entrypoint unchanged (gateway injects them). Playwright **browsers** are not
bundled; the `playwright` package is installed for import readiness only.

## Build

From the repo root (after a host GUI build):

```bash
Webservice/image/stage.sh
podman build -t localhost/chudware-session:latest \
  -f Webservice/image/Containerfile Webservice/image
```

`stage/` is intentionally not committed. Driver should add:

```
Webservice/image/stage/
```

to the repo `.gitignore`.

## Contract env (gateway injects)

| Variable | Meaning |
|----------|---------|
| `CHUDWARE_USER_EMAIL` / `CHUDWARE_USER_DISPLAY` | Portal identity |
| `CHUDWARE_USER_ID` / `CHUDWARE_USER_DISPLAY` | Vault author injection |
| `STREAM_BACKEND` | `novnc` (default) or `selkies` |
| `SELKIES_ENCODER` | `x264enc` (default) or `nvh264enc` |
| `CHUDWARE_HEAVY` | `1` → try VirtualGL EGL when NVIDIA devices present |
| `DISPLAY` | `:0` |
| `CHUDWARE_PLM_DIR` | `/plm` (shared bare repos mount) |
| `CHUDWARE_LIBCHUDWARE` | `/opt/chudware/bin/libchudware.so` (set in image) |
| `CHUDWARE_PYTHON` / `CHUDWARE_REPO` / `CHUDWARE_PROMPTS_DIR` | Worker runtime (set in image) |
| `*_API_KEY` | LLM provider keys — pass-through, never stripped |

Volumes: private `…/homes/<slug>:/home/user`, shared `…/plm:/plm`.

## PLM bootstrap (T-WEB2 §Q2)

Host seeds bare remotes once (outside the image):

```bash
cd Webservice/gateway
python3 -m plm_seed Demo          # → ~/chudware-web-data/plm/Demo.git (real tip)
```

On seat start, `entrypoint.sh` loops `/plm/*.git`:

- no `$HOME/Projects/<name>/.aicadvault` → `clone_from_remote` (log `[plm] cloned <name>`)
- vault present → **pull/FF only** (log `[plm] synced <name>`); **never** auto-push

Failures log `[plm] WARN …` and continue so the seat still boots. Publish is
explicit user/agent `plm_sync` only — no stop-time sync.


## Smoke (verified 2026-07-14)

```bash
TMPHOME=$(mktemp -d); chmod 777 "$TMPHOME"
CID=$(podman run --rm -d \
  -p 127.0.0.1:8480:8080 \
  -v "$TMPHOME:/home/user:Z" \
  -e CHUDWARE_USER_EMAIL=smoke@example.com \
  -e CHUDWARE_USER_DISPLAY=Smoke \
  -e CHUDWARE_USER_ID=smoke@example.com \
  -e CHUDWARE_USER_DISPLAY=Smoke \
  -e STREAM_BACKEND=novnc \
  localhost/chudware-session:latest)

for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8480/ >/tmp/novnc.html && break
  sleep 1
done
grep -ioE '<title>[^<]+</title>' /tmp/novnc.html   # → <title>noVNC</title>

podman exec "$CID" bash -c \
  'xwd -display :0 -root -out /tmp/root.xwd && magick /tmp/root.xwd /tmp/root.png && ls -la /tmp/root.png'

podman stop "$CID"
rm -rf "$TMPHOME"
```

Observed: HTTP 200 from websockify, noVNC HTML, Chudware + chudware-hostd running,
`[engine] daemon mode: connected to chudware-hostd`, worker
`/opt/chudware/runtime/venv/bin/python -m engine.worker` auto-starts, **no**
`worker not started` toast, AI dock usable, status bar **Ready.**
Image size ~5.9 GB (was ~4.1 GB before Python runtime).

## Selkies status

`pip install selkies==1.6.1` **fails** on Fedora 44 without kernel headers
(`evdev` needs `linux/input.h`). Even when installed, that wheel is the legacy
**GStreamer** WebRTC frontend (`selkies-gstreamer`), not modern pixelflux. The
image records `/opt/chudware-selkies.status` and **defaults to novnc** (verified).

## Open issues / notes

- HOME volume: host dir must be writable by uid 1000 (`chmod 777` or `:U`).
- Python worker runtime is image-layer (`/opt/chudware/runtime` + `/opt/chudware/app`);
  readiness probe imports (build123d, pyvista, mcp, anthropic, openai, pptx, …)
  must succeed. Playwright Chromium binaries are not installed.
- Missing `hostname` binary → harmless `novnc_proxy` warning (image now includes it).
- Image ~5+ GB after Python venv (CEF stripped + OCCT + Mesa + VirtualGL + wheels).
