# billet-session image

Per-user streamed Billet CAD seat for billet-web.

- **Base:** `registry.fedoraproject.org/fedora:44` (glibc match with host build)
- **Tag:** `localhost/billet-session:latest`
- **Stream port:** `8080` (HTTP + WS)
- **Default backend:** `STREAM_BACKEND=novnc` (x11vnc + websockify + noVNC)
- **Optional:** `STREAM_BACKEND=selkies` if `selkies` pip install succeeded at build

## Why Fedora (not Selkies Ubuntu GHCR)

Host Billet is built against **glibc 2.43** + system **OCCT 7.9** on Fedora 44.
`ghcr.io/selkies-project/nvidia-glx-desktop:24.04` is Ubuntu 24.04 (glibc 2.39) and
was **rejected** as a base (advisor Q4 / DESIGN.md).

## CEF note (advisor §A6)

There is **no runtime flag** to disable the vendor browser. `AI_CAD_ENABLE_CEF` is
compile-time only; the linked `Billet` binary `NEEDED`s `libcef.so` and
`CefInitialize` aborts without `icudtl.dat` / `*.pak` next to the exe. The image
therefore ships a **stripped** `libcef.so` (~417 MiB) + minimal CEF runtime.
`AI_CAD_CEF_LAZY=1` defers cold init until a web surface opens.

## Files

| Path | Role |
|------|------|
| `stage.sh` | Assemble `stage/` from `host/build-linux/bin` + hostd + netgen + CEF |
| `stage/` | **Build output only** — do not commit (gitignore) |
| `Containerfile` | Fedora 44 image |
| `entrypoint.sh` | Xvfb → Billet → novnc/selkies; SIGTERM cleanup |

## Build

From the repo root (after a host GUI build):

```bash
Webservice/image/stage.sh
podman build -t localhost/billet-session:latest \
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
| `BILLET_USER_EMAIL` / `BILLET_USER_DISPLAY` | Portal identity |
| `AICAD_USER_ID` / `AICAD_USER_DISPLAY` | Vault author injection |
| `STREAM_BACKEND` | `novnc` (default) or `selkies` |
| `SELKIES_ENCODER` | `x264enc` (default) or `nvh264enc` |
| `BILLET_HEAVY` | `1` → try VirtualGL EGL when NVIDIA devices present |
| `DISPLAY` | `:0` |
| `BILLET_PLM_DIR` | `/plm` (shared bare repos mount) |

Volumes: private `…/homes/<slug>:/home/user`, shared `…/plm:/plm`.

## Smoke

```bash
TMPHOME=$(mktemp -d)
CID=$(podman run --rm -d \
  -p 127.0.0.1:8480:8080 \
  -v "$TMPHOME:/home/user:Z" \
  -e BILLET_USER_EMAIL=smoke@example.com \
  -e BILLET_USER_DISPLAY=Smoke \
  -e AICAD_USER_ID=smoke@example.com \
  -e AICAD_USER_DISPLAY=Smoke \
  -e STREAM_BACKEND=novnc \
  localhost/billet-session:latest)

# Stream UI HTML within ~60s
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8480/ >/tmp/novnc.html && break
  sleep 1
done
head -c 200 /tmp/novnc.html

# Billet drawing on the X display
podman exec "$CID" bash -c \
  'xwd -display :0 -root -out /tmp/root.xwd && convert /tmp/root.xwd /tmp/root.png && ls -la /tmp/root.png'

podman stop "$CID"
rm -rf "$TMPHOME"
```

## Selkies status

`pip install selkies==1.6.1` installs the legacy **GStreamer** WebRTC frontend
(`selkies-gstreamer`). It is **not** the modern pixelflux stack used by the
official Selkies desktop images, and on Fedora it often lacks a complete capture
pipeline without extra GStreamer plugins. The image records
`/opt/billet-selkies.status` and **defaults to novnc**, which is verified
working. Prefer `STREAM_BACKEND=novnc` for demos until a Fedora-native Selkies
path is proven.
