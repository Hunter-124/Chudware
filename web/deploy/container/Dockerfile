# syntax=docker/dockerfile:1
# The source archive is intentionally not a signed desktop release: developer
# execution remains explicit, with no fabricated protected-assets manifest.
ARG FEDORA_IMAGE=registry.fedoraproject.org/fedora:44
ARG EMSDK_IMAGE=docker.io/emscripten/emsdk:6.0.3
ARG CADDY_IMAGE=docker.io/library/caddy:2.10.2-alpine

FROM ${FEDORA_IMAGE} AS native
ARG BUILD_JOBS=2
ENV CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS} \
    GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1
# Match install-fedora-deps.sh's native kernel/toolchain, without its native
# GLFW/Wayland/AppIndicator/CEF GUI dependencies. No engine feature is cut out.
RUN dnf -y install --setopt=install_weak_deps=False \
        gcc-c++ cmake ninja-build git make pkgconf-pkg-config binutils \
        python3 python3.13 opencascade-devel suitesparse-devel libcurl-devel \
        gperftools-devel tar xz bzip2 zstd zip unzip curl file which findutils \
    && dnf clean all
WORKDIR /src
# Only compile inputs precede the expensive native build. Keep whole source
# trees: core's CMake also declares targets using host test/render and service
# sources, even when only the full-engine application targets are requested.
COPY core/ ./core/
COPY host/ ./host/
COPY services/ ./services/
RUN cmake -S core -B core/build-occt-linux -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCHUDWARE_DEVELOPER_BUILD=ON \
        -DCHUDWARE_WITH_OCCT=ON \
        -DCHUDWARE_WITH_CHOLMOD=ON \
        -DCHUDWARE_WITH_LIB3MF=ON \
        -DCHUDWARE_WITH_OPENNURBS=ON \
        -DCHUDWARE_WITH_POLYMESH=ON \
    && cmake --build core/build-occt-linux --target chudware chudware-hostd polymesh
# Runtime packaging is deliberately a later layer: fixing stage.py or a launcher
# must reuse the completed native compilation in Docker and Podman alike.
COPY assets/ ./assets/
COPY contracts/ ./contracts/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY web/gateway/ ./web/gateway/
COPY web/deploy/cell/ ./web/deploy/cell/
COPY web/deploy/container/ ./web/deploy/container/
RUN python3 web/deploy/container/stage.py payload /src /src/core/build-occt-linux /out

FROM ${EMSDK_IMAGE} AS wasm
ARG BUILD_JOBS=2
ENV EMSDK=/emsdk CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS} \
    CHUDWARE_WEB_APP=/out/app GIT_TERMINAL_PROMPT=0
RUN apt-get update \
    && apt-get install -y --no-install-recommends cmake ninja-build python3 brotli gzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY core/ ./core/
COPY host/ ./host/
COPY services/ ./services/
COPY extern/glad/ ./extern/glad/
COPY assets/ ./assets/
COPY build.sh ./
COPY scripts/check_web_parity.py ./scripts/check_web_parity.py
# Reuse the canonical full-WASM build, immutable bundle URL stamping, preloaded
# assets and compression staging; do not maintain a second browser-bundle recipe.
RUN bash ./build.sh wasm
# Notice collection changes do not invalidate the canonical build/staging layer.
COPY web/deploy/container/stage.py ./web/deploy/container/stage.py
RUN python3 web/deploy/container/stage.py wasm-notices /src/host/build-wasm /out/wasm-licenses

FROM ${FEDORA_IMAGE} AS runtime-base
RUN dnf -y install --setopt=install_weak_deps=False \
        python3 python3.13 bubblewrap ca-certificates tar gzip \
        ngspice mesa-libGL mesa-libEGL mesa-dri-drivers libX11 libXrender libXext \
    && dnf clean all

FROM runtime-base AS python-runtime
RUN dnf -y install --setopt=install_weak_deps=False \
        python3.13-devel python3-pip gcc-c++ make pkgconf-pkg-config \
    && dnf clean all
WORKDIR /src
COPY requirements.txt ./requirements.txt
COPY web/gateway/requirements.txt ./gateway-requirements.txt
COPY web/deploy/container/stage.py ./stage.py
# Exactly one optional hash-pinned SDK archive line is removed. All other root
# requirements, including non-AI Python workers, are installed unchanged. The
# browser's credential-isolated Copilot remains unavailable; no native agent
# runtime is fetched, no Git credentials are needed, and SDK imports stay lazy.
RUN python3.13 stage.py requirements requirements.txt public-requirements.txt \
    && python3.13 -m venv --copies /opt/chudware/venv \
    && /opt/chudware/venv/bin/python -m pip install --no-cache-dir \
        -r public-requirements.txt -r gateway-requirements.txt
RUN python3.13 stage.py python-packages /opt/chudware/venv /python-packages.txt

# Independent target: building the authenticated TLS proxy never builds CAD.
FROM ${CADDY_IMAGE} AS https-proxy
USER root
# High internal ports need no file capability. Retaining the upstream binary's
# CAP_NET_BIND_SERVICE makes exec fail under cap_drop: ALL.
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /data/caddy /config/caddy \
    && chown -R 10001:10001 /data /config
COPY web/deploy/Caddyfile /etc/caddy/Caddyfile
USER 10001:10001

FROM runtime-base AS runtime
# Install complete RPMs, preserving runtime dlopen plugins/configuration and
# /usr/share/licenses rather than transplanting a bare dependency .so closure.
COPY --from=native /out/runtime-packages.txt /tmp/native-packages.txt
COPY --from=python-runtime /python-packages.txt /tmp/python-packages.txt
RUN dnf -y install --setopt=install_weak_deps=False \
        $(cat /tmp/native-packages.txt /tmp/python-packages.txt) \
    && dnf clean all \
    && rm /tmp/native-packages.txt /tmp/python-packages.txt \
    && mkdir -p /data /work /state /run/chudware \
    && chown 10001:10001 /data /work /state /run/chudware \
    && chmod 0700 /data /work /state /run/chudware
COPY --from=native /out/chudware/ /opt/chudware/
COPY --from=python-runtime /opt/chudware/venv/ /opt/chudware/venv/
COPY --from=wasm /out/app/ /opt/chudware/app/
COPY --from=wasm /out/wasm-licenses/ /opt/chudware/third-party-licenses/wasm/
ENV CHUDWARE_CONTAINER_RUNTIME=1 \
    CHUDWARE_WEB_CELL_BACKEND=container \
    CHUDWARE_WEB_HOME=/data \
    CHUDWARE_WORKSPACE_ROOT=/work \
    CHUDWARE_WEB_APP=/opt/chudware/app \
    CHUDWARE_WORKER_PYTHON=/opt/chudware/venv/bin/python \
    HOME=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    PATH=/opt/chudware/bin:/opt/chudware/venv/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH=/opt/chudware/bin
WORKDIR /opt/chudware
USER 10001:10001
EXPOSE 8300
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=12s --start-period=180s --retries=3 \
    CMD ["/opt/chudware/venv/bin/python", "/opt/chudware/web/deploy/container/runtime.py", "healthcheck"]
ENTRYPOINT ["/opt/chudware/venv/bin/python", "/opt/chudware/web/deploy/container/runtime.py"]
