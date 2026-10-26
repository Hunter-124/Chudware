#!/usr/bin/env bash
# Install the Fedora packages the Linux CI jobs need. The jobs run in a
# fedora:44 container because the core requires OCCT 7.9.x, which Fedora ships
# as opencascade-devel and Ubuntu does not.
#
#   install-fedora-deps.sh            native core + host build and ctest
#   install-fedora-deps.sh gate       + scripts/gate.sh extras (Xvfb, ngspice, CEF runtime)
#   install-fedora-deps.sh release    + packaging tools (AppImage, .deb, .rpm, Nuitka)
#
# Developers on Fedora can run the same script with sudo to provision a
# workstation that matches CI.
set -euo pipefail

packages=(
    # Toolchain
    gcc-c++ cmake ninja-build git make pkgconf-pkg-config binutils
    # The product virtualenvs use Python 3.13: every pinned requirement ships cp313 wheels.
    python3 python3-pip python3.13 python3.13-devel
    # Core libraries
    opencascade-devel suitesparse-devel libcurl-devel gperftools-devel
    # GLFW (X11 and Wayland backends), OpenGL, hostd tray
    libX11-devel libXrandr-devel libXinerama-devel libXcursor-devel libXi-devel libXext-devel
    libxkbcommon-devel wayland-devel wayland-protocols-devel mesa-libGL-devel mesa-libEGL-devel
    libappindicator-gtk3-devel
    # Archive and inspection tools used by fetch scripts and tests
    tar xz bzip2 zstd zip unzip curl file which findutils hostname procps-ng openssl
)

cef_runtime=(
    nss nspr atk at-spi2-atk cups-libs libdrm mesa-libgbm alsa-lib pango cairo
    libXcomposite libXdamage libXfixes libxshmfence dbus-libs
)

for profile in "$@"; do
    case "${profile}" in
        gate)
            packages+=("${cef_runtime[@]}" xorg-x11-server-Xvfb xorg-x11-xauth mesa-dri-drivers
                       ngspice rpm-build dpkg)
            ;;
        release)
            packages+=("${cef_runtime[@]}" xorg-x11-server-Xvfb xorg-x11-xauth mesa-dri-drivers
                       patchelf rsync rpm-build dpkg desktop-file-utils gtk-update-icon-cache)
            ;;
        *)
            echo "unknown profile: ${profile} (expected: gate, release)" >&2
            exit 2
            ;;
    esac
done

dnf -y install --setopt=install_weak_deps=False "${packages[@]}"
dnf clean all
