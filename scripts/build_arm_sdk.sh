#!/bin/bash
# Build the ARM dependency SDK the cross presets link against, from pinned
# sources, into a private per-account prefix:
#
#   scripts/build_arm_sdk.sh linux-aarch64 [ROOT]   (x86_64 Linux build host)
#   scripts/build_arm_sdk.sh darwin-arm64  [ROOT]   (Intel Mac build host)
#
# Prints the SDK prefix, alone, on stdout (all build output goes to stderr);
# export it as CHUDWARE_ARM_SDK for the linux-aarch64-qemu / macos-arm64-cross
# presets.
#
# ROOT defaults to $RUNNER_TOOL_CACHE/chudware-arm-sdk on a runner (each
# product's runner account has its own tool cache), else
# ${XDG_CACHE_HOME:-$HOME/.cache}/chudware-arm-sdk. The prefix is
# ROOT/<target>/<recipe id>, where the id digests this script and the target's
# toolchain and compiler/SDK identity, so changing a pin or a flag builds a fresh
# SDK. A .complete stamp is published atomically only after an actual consumer
# links every required OCCT export and passes architecture checks (and, on Linux,
# executes under QEMU). Completed caches must pass the same checks before reuse.
#
# Contents (all target-architecture objects):
#   OCCT       V7_9_3 @ a016080b, the same commit the native Linux CI builds
#              (OCCT_TAG/OCCT_COMMIT below);
#              shared, FreeType + RapidJSON on. Built without Xlib/OpenGL/Tk/
#              Draw (core links none of TKOpenGl/TKDraw) and without TBB
#              (OCCT then uses its own OSD_ThreadPool).
#   FreeType   static, PIC, no optional codecs (OCCT text/fonts: core/geom/text.cpp)
#   RapidJSON  headers (OCCT glTF reader/writer)
#   fontconfig linux-aarch64 only (OCCT's Linux font manager requires it);
#              shared, Expat and FreeType linked in, configuration under the
#              prefix's etc/fonts, fonts from /usr/share/fonts
#   OpenSSL    linux-aarch64 only; static, linked into libcurl
#   curl       linux-aarch64 only; shared libcurl over OpenSSL (the macOS SDK
#              already ships an arm64 libcurl)
# Shared libraries carry the prefix's lib directory as their run path, so a
# consumer finds their dependencies without LD_LIBRARY_PATH.
#
# The host needs git, cmake >= 3.24, ninja and python3. linux-aarch64 also needs
# perl, make, curl, pkg-config, gperf, the GNU cross compiler (aarch64-linux-gnu-gcc/
# g++, cross libraries in /usr/aarch64-linux-gnu) and qemu-aarch64(-static);
# darwin-arm64 needs the Command Line Tools. Bash 3.2 compatible (macOS
# /bin/bash).
#
# Every fetch, configure, build and install runs under scripts/run_bounded.py
# (macOS has no GNU timeout): a step that overruns its budget below has its
# whole process group stopped and the script fails with status 124; a failing
# step fails it with that step's status. Bound the whole run too (native-build
# gives linux-aarch64 60 minutes and darwin-arm64 230 minutes).
set -euo pipefail
# fd 3 is the caller's stdout and carries only the prefix.
exec 3>&1 1>&2

OCCT_TAG=V7_9_3
OCCT_COMMIT=a016080bf6738d6aeae020badee4e888ad1540a5
FREETYPE_TAG=VER-2-14-3
FREETYPE_COMMIT=0a0221a1347e2f1e07c395263540026e9a0aa7c7
RAPIDJSON_COMMIT=24b5e7a8b27f42fa16b96fc70aade9106cf7102f
OPENSSL_TAG=openssl-3.5.9
OPENSSL_COMMIT=45e844fa2a14ec92d146bd8f5778ac130b6625fb
CURL_TAG=curl-8_22_0
CURL_COMMIT=01346829096c61b372692f6dc43ffa778c6caccd
EXPAT_TAG=R_2_8_5
EXPAT_COMMIT=4b3f0b06f39fb5529cead381694f8929901bc273
# Release tarball: its pre-generated configure needs no autotools on the host.
FONTCONFIG_VERSION=2.16.0
FONTCONFIG_SHA256=6a33dc555cc9ba8b10caf7695878ef134eeb36d0af366041f639b1da9b6ed220

# Per-step wall-clock budgets, in seconds. Only OCCT needs the longer Darwin build.
FETCH_SECONDS=600
CONFIGURE_SECONDS=900
BUILD_SECONDS=1800
INSTALL_SECONDS=600

die() { printf 'build_arm_sdk: %s\n' "$*" >&2; exit 1; }
log() { printf '[build_arm_sdk] %s\n' "$*" >&2; }

[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: $0 linux-aarch64|darwin-arm64 [ROOT]"
target=$1
repo=$(cd "$(dirname "$0")/.." && pwd)
case "$target" in
  linux-aarch64)
    [ "$(uname -s)" = Linux ] || die "linux-aarch64 is built on a Linux host"
    toolchain=$repo/cmake/toolchains/aarch64-linux-gnu.cmake
    OCCT_BUILD_SECONDS=$BUILD_SECONDS ;;
  darwin-arm64)
    [ "$(uname -s)" = Darwin ] || die "darwin-arm64 is built on a Mac"
    toolchain=$repo/cmake/toolchains/darwin-arm64.cmake
    # The Intel Mac test account builds with one compiler process
    # (CMAKE_BUILD_PARALLEL_LEVEL=1). Cold, OCCT reached 1241 of 5476 Ninja
    # actions in 30 minutes (about 1.45 s each, slowly rising), so its build
    # alone needs about 133 minutes; 200 minutes allow 2.2 s per action.
    OCCT_BUILD_SECONDS=12000 ;;
  *) die "unknown target '$target' (linux-aarch64|darwin-arm64)" ;;
esac

if [ -n "${2:-}" ]; then
  root=$2
elif [ -n "${RUNNER_TOOL_CACHE:-}" ]; then
  root=$RUNNER_TOOL_CACHE/chudware-arm-sdk
else
  root=${XDG_CACHE_HOME:-$HOME/.cache}/chudware-arm-sdk
fi

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; }

for tool in git cmake ninja python3; do
  command -v "$tool" >/dev/null 2>&1 || die "missing host tool: $tool"
done
bounded() {
  if [ -n "${CHUDWARE_JOB_DEADLINE:-}" ]; then
    python3 "$repo/scripts/run_bounded.py" --not-after "$CHUDWARE_JOB_DEADLINE" "$@"
  else
    python3 "$repo/scripts/run_bounded.py" "$@"
  fi
}
if [ "$target" = linux-aarch64 ]; then
  for tool in perl make curl pkg-config gperf aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ aarch64-linux-gnu-readelf; do
    command -v "$tool" >/dev/null 2>&1 || die "missing host tool: $tool"
  done
  tool_identity=$(bounded "$CONFIGURE_SECONDS" aarch64-linux-gnu-g++ -v 2>&1)
else
  for tool in xcrun lipo; do
    command -v "$tool" >/dev/null 2>&1 || die "missing host tool: $tool"
  done
  tool_identity=$(bounded "$CONFIGURE_SECONDS" xcrun --sdk macosx clang++ --version)
  macos_sdk=$(bounded "$CONFIGURE_SECONDS" xcrun --sdk macosx --show-sdk-path)
  tool_identity="$tool_identity $macos_sdk $(bounded "$CONFIGURE_SECONDS" xcrun --sdk macosx --show-sdk-version)"
fi

# Do not accept a shared or another account's writable cache. No system prefixes
# or package manager installations are modified by this recipe.
[ "$(id -u)" -ne 0 ] || die "run as the product's unprivileged runner account"
umask 077
mkdir -p "$root"
root=$(cd "$root" && pwd -P)
[ -O "$root" ] || die "cache root is not owned by this account: $root"
chmod 700 "$root"
[ ! -L "$root/$target" ] || die "target cache must not be a symlink"
mkdir -p "$root/$target"
[ -O "$root/$target" ] || die "target cache is not owned by this account"
chmod 700 "$root/$target"
lock=$root/$target/.lock
mkdir "$lock" 2>/dev/null || die "SDK cache is locked: $lock (remove only after its builder has stopped)"
probe=
trap '[ -z "$probe" ] || rm -rf "$probe"; rmdir "$lock"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
id=$({ cat "$0" "$toolchain" "$repo/cmake/toolchains/qemu-aarch64-run.sh"; printf '%s\n' "$tool_identity"; } | sha256 | cut -c1-16)
prefix=$root/$target/$id
[ ! -L "$prefix" ] || die "SDK prefix must not be a symlink"
if [ -e "$prefix" ]; then
  [ -O "$prefix" ] || die "SDK prefix is not owned by this account"
fi
export CHUDWARE_ARM_SDK=$prefix

jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-}
if [ -z "$jobs" ]; then
  jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu)
fi

# The SDK prefix is also the target toolchain's dependency search root.
# A cache consumer probe is constructed below before deciding whether to reuse it.

# fetch DIR URL REF COMMIT: shallow-fetch REF (a tag, or the commit itself) and
# refuse anything but COMMIT.
fetch() {
  bounded "$FETCH_SECONDS" git init -q "$1"
  bounded "$FETCH_SECONDS" git -C "$1" fetch -q --depth 1 "$2" "$3"
  bounded "$FETCH_SECONDS" git -C "$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
  [ "$(git -C "$1" rev-parse HEAD)" = "$4" ] || die "$2 $3 is not commit $4"
}

cmake_target() { # SRC BUILD SECONDS [ARGS...]: configure, build and install with the target toolchain
  local src=$1 build=$2 build_seconds=$3
  shift 3
  bounded "$CONFIGURE_SECONDS" cmake -S "$src" -B "$build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" -DCHUDWARE_ARM_SDK="$prefix" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_INSTALL_RPATH="$prefix/lib" -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=OFF \
    -DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF -DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF "$@"
  bounded "$build_seconds" cmake --build "$build" --parallel "$jobs"
  bounded "$INSTALL_SECONDS" cmake --install "$build"
}

validate_sdk() {
  [ -f "$prefix/lib/cmake/opencascade/OpenCASCADEConfig.cmake" ] || return 1
  probe=$(mktemp -d "$root/$target/.probe.XXXXXXXX")
  cat >"$probe/CMakeLists.txt" <<'CMAKE'
cmake_minimum_required(VERSION 3.24)
project(chudware_arm_sdk_consumer LANGUAGES C CXX)
find_package(OpenCASCADE 7.9.3 EXACT REQUIRED CONFIG)
set(required
  TKernel TKMath TKG2d TKG3d TKGeomBase TKGeomAlgo TKBRep TKTopAlgo TKPrim
  TKMesh TKShHealing TKBO TKFillet TKHLR TKOffset TKV3d TKService
  TKDESTEP TKDEIGES TKDESTL TKXSBase TKRWMesh TKDEOBJ TKDEGLTF
  TKXCAF TKLCAF TKCDF TKBin TKBinL TKBinXCAF)
foreach(t IN LISTS required OpenCASCADE_LIBRARIES)
  if(NOT TARGET ${t})
    message(FATAL_ERROR "Missing exported OCCT target ${t}")
  endif()
  get_target_property(configs ${t} IMPORTED_CONFIGURATIONS)
  foreach(config IN LISTS configs)
    string(TOUPPER "${config}" config)
    get_target_property(location ${t} IMPORTED_LOCATION_${config})
    if(NOT EXISTS "${location}")
      message(FATAL_ERROR "Missing installed OCCT library ${t}: ${location}")
    endif()
    file(REAL_PATH "${location}" location)
    cmake_path(IS_PREFIX CHUDWARE_ARM_SDK "${location}" NORMALIZE in_sdk)
    if(NOT in_sdk)
      message(FATAL_ERROR "OCCT target ${t} escaped the ARM SDK: ${location}")
    endif()
  endforeach()
endforeach()
find_package(Freetype REQUIRED)
find_package(CURL REQUIRED)
add_executable(sdk_consumer consumer.cpp)
target_include_directories(sdk_consumer PRIVATE ${OpenCASCADE_INCLUDE_DIR})
target_link_libraries(sdk_consumer PRIVATE ${OpenCASCADE_LIBRARIES}
  Freetype::Freetype CURL::libcurl)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  find_library(FONTCONFIG_LIBRARY fontconfig REQUIRED)
  target_link_libraries(sdk_consumer PRIVATE ${FONTCONFIG_LIBRARY})
  target_compile_definitions(sdk_consumer PRIVATE CHUDWARE_SDK_LINUX)
  target_link_options(sdk_consumer PRIVATE -Wl,--no-as-needed)
endif()
CMAKE
  cat >"$probe/consumer.cpp" <<'CPP'
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <GProp_GProps.hxx>
#include <StdPrs_BRepFont.hxx>
#include <StdPrs_BRepTextBuilder.hxx>
#include <RWGltf_CafReader.hxx>
#include <RWGltf_CafWriter.hxx>
#include <XCAFApp_Application.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <TDocStd_Document.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <Message_ProgressRange.hxx>
#include <ft2build.h>
#include FT_FREETYPE_H
#include <curl/curl.h>
#include <cmath>
#ifdef CHUDWARE_SDK_LINUX
extern "C" int FcInit(void);
#endif
int main() {
  const TopoDS_Shape box = BRepPrimAPI_MakeBox(2., 3., 4.).Shape();
  GProp_GProps props;
  BRepGProp::VolumeProperties(box, props);
  BRepMesh_IncrementalMesh mesher(box, 0.1);
  StdPrs_BRepFont font;
  StdPrs_BRepTextBuilder text;
  Handle(TDocStd_Document) doc;
  const auto app = XCAFApp_Application::GetApplication();
  app->NewDocument("BinXCAF", doc);
  if (doc.IsNull()) return 1;
  XCAFDoc_DocumentTool::ShapeTool(doc->Main())->AddShape(box, Standard_False);
  TColStd_IndexedDataMapOfStringString info;
  RWGltf_CafWriter writer("chudware-sdk-probe.glb", true);
  if (!writer.Perform(doc, info, Message_ProgressRange())) return 7;
  Handle(TDocStd_Document) imported;
  app->NewDocument("BinXCAF", imported);
  if (imported.IsNull()) return 8;
  RWGltf_CafReader reader;
  reader.SetDocument(imported);
  reader.SetParallel(Standard_False);
  if (!reader.Perform("chudware-sdk-probe.glb", Message_ProgressRange()) ||
      reader.SingleShape().IsNull()) return 9;
  FT_Library ft = nullptr;
  if (FT_Init_FreeType(&ft) != 0) return 2;
  FT_Done_FreeType(ft);
  if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 3;
  CURL* curl = curl_easy_init();
  if (!curl) return 4;
  curl_easy_cleanup(curl);
  curl_global_cleanup();
#ifdef CHUDWARE_SDK_LINUX
  if (!FcInit()) return 5;
#endif
  app->Close(doc);
  app->Close(imported);
  return mesher.IsDone() && std::fabs(props.Mass() - 24.) < 1e-6 ? 0 : 6;
}
CPP
  bounded "$CONFIGURE_SECONDS" cmake -S "$probe" -B "$probe/build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" -DCHUDWARE_ARM_SDK="$prefix" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_BUILD_RPATH="$prefix/lib" \
    -DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF -DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF || return $?
  bounded "$BUILD_SECONDS" cmake --build "$probe/build" --parallel "$jobs" || return $?
  bounded "$CONFIGURE_SECONDS" python3 - "$target" "$prefix" "$probe/build/sdk_consumer" <<'PY' || return $?
import pathlib, re, subprocess, sys
target, prefix, consumer = sys.argv[1:]
libraries = [p for p in pathlib.Path(prefix, "lib").iterdir()
             if p.is_file() and (p.name.endswith((".a", ".dylib", ".so")) or ".so." in p.name)]
if not libraries:
    raise SystemExit("SDK installed no target libraries")
for path in libraries + [pathlib.Path(consumer)]:
    if target == "darwin-arm64":
        arch = subprocess.check_output(["lipo", "-archs", str(path)], text=True).strip()
        if arch != "arm64":
            raise SystemExit(f"{path}: expected thin arm64, got {arch}")
    else:
        headers = subprocess.check_output(["aarch64-linux-gnu-readelf", "-h", str(path)], text=True)
        machines = re.findall(r"^\s*Machine:\s*(.+)$", headers, re.M)
        if not machines or any(m != "AArch64" for m in machines):
            raise SystemExit(f"{path}: expected only AArch64 ELF objects, got {machines}")
PY
  if [ "$target" = linux-aarch64 ]; then
    qemu=$(command -v qemu-aarch64-static || command -v qemu-aarch64) || return 1
    (cd "$probe" && bounded 600 "$repo/cmake/toolchains/qemu-aarch64-run.sh" \
      "$qemu" /usr/aarch64-linux-gnu "$probe/build/sdk_consumer") || return $?
  fi
  rm -rf "$probe"
  probe=
}

if [ -f "$prefix/.complete" ]; then
  log "validating cached SDK $prefix"
  cache_status=0
  validate_sdk || cache_status=$?
  if [ "$cache_status" -eq 0 ]; then
    log "reusing $prefix"
    printf '%s\n' "$prefix" >&3
    exit 0
  fi
  [ "$cache_status" -ne 124 ] || exit 124
  log "discarding unusable SDK $prefix"
  [ -z "$probe" ] || rm -rf "$probe"
  probe=
fi
rm -rf "$prefix"
mkdir -p "$prefix/.work"
work=$prefix/.work

if [ "$target" = linux-aarch64 ]; then
  log "OpenSSL $OPENSSL_TAG"
  fetch "$work/openssl" https://github.com/openssl/openssl.git "refs/tags/$OPENSSL_TAG" "$OPENSSL_COMMIT"
  # /usr/lib/ssl is Ubuntu's OPENSSLDIR; under qemu the lookup falls through
  # the cross sysroot to the build host's CA store and openssl.cnf.
  (cd "$work/openssl" &&
    bounded "$CONFIGURE_SECONDS" ./Configure linux-aarch64 --cross-compile-prefix=aarch64-linux-gnu- \
      --prefix="$prefix" --libdir=lib --openssldir=/usr/lib/ssl \
      no-shared no-module no-tests -fPIC &&
    bounded "$BUILD_SECONDS" make -j"$jobs" build_sw >/dev/null &&
    bounded "$INSTALL_SECONDS" make install_sw >/dev/null)

  log "curl $CURL_TAG"
  fetch "$work/curl" https://github.com/curl/curl.git "refs/tags/$CURL_TAG" "$CURL_COMMIT"
  cmake_target "$work/curl" "$work/curl-build" "$BUILD_SECONDS" \
    -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF -DBUILD_CURL_EXE=OFF -DBUILD_TESTING=OFF \
    -DBUILD_LIBCURL_DOCS=OFF -DBUILD_MISC_DOCS=OFF -DENABLE_CURL_MANUAL=OFF \
    -DCURL_USE_OPENSSL=ON -DOPENSSL_ROOT_DIR="$prefix" -DOPENSSL_USE_STATIC_LIBS=ON \
    -DCURL_ZLIB=OFF -DCURL_BROTLI=OFF -DCURL_ZSTD=OFF -DUSE_NGHTTP2=OFF -DUSE_LIBIDN2=OFF \
    -DCURL_USE_LIBPSL=OFF -DCURL_USE_LIBSSH2=OFF \
    -DCURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt -DCURL_CA_PATH=/etc/ssl/certs
fi

log "FreeType $FREETYPE_TAG"
fetch "$work/freetype" https://gitlab.freedesktop.org/freetype/freetype.git "refs/tags/$FREETYPE_TAG" "$FREETYPE_COMMIT"
cmake_target "$work/freetype" "$work/freetype-build" "$BUILD_SECONDS" \
  -DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DFT_DISABLE_ZLIB=ON -DFT_DISABLE_BZIP2=ON -DFT_DISABLE_PNG=ON \
  -DFT_DISABLE_HARFBUZZ=ON -DFT_DISABLE_BROTLI=ON

occt_link=()
if [ "$target" = linux-aarch64 ]; then
  log "Expat $EXPAT_TAG"
  fetch "$work/expat" https://github.com/libexpat/libexpat.git "refs/tags/$EXPAT_TAG" "$EXPAT_COMMIT"
  cmake_target "$work/expat/expat" "$work/expat-build" "$BUILD_SECONDS" \
    -DEXPAT_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DEXPAT_BUILD_TOOLS=OFF -DEXPAT_BUILD_EXAMPLES=OFF -DEXPAT_BUILD_TESTS=OFF \
    -DEXPAT_BUILD_DOCS=OFF -DEXPAT_BUILD_FUZZERS=OFF

  log "fontconfig $FONTCONFIG_VERSION"
  tarball=$work/fontconfig-$FONTCONFIG_VERSION.tar.xz
  bounded "$FETCH_SECONDS" curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 30 --retry 3 --output "$tarball" \
    "https://www.freedesktop.org/software/fontconfig/release/fontconfig-$FONTCONFIG_VERSION.tar.xz"
  printf '%s  %s\n' "$FONTCONFIG_SHA256" "$tarball" | sha256sum --check --strict --quiet ||
    die "fontconfig $FONTCONFIG_VERSION digest mismatch"
  bounded "$INSTALL_SECONDS" tar -xJf "$tarball" -C "$work"
  (cd "$work/fontconfig-$FONTCONFIG_VERSION" &&
    bounded "$CONFIGURE_SECONDS" env CC=aarch64-linux-gnu-gcc AR=aarch64-linux-gnu-ar \
      RANLIB=aarch64-linux-gnu-ranlib PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig" PKG_CONFIG_PATH='' \
      ./configure --host=aarch64-linux-gnu --prefix="$prefix" --libdir="$prefix/lib" \
      --sysconfdir="$prefix/etc" --localstatedir="$prefix/var" \
      --with-default-fonts=/usr/share/fonts --enable-shared --disable-static --with-pic \
      --disable-docs --disable-nls --disable-cache-build --disable-iconv \
      LDFLAGS="-Wl,-rpath,$prefix/lib" >/dev/null &&
    bounded "$BUILD_SECONDS" make -j"$jobs" >/dev/null &&
    bounded "$INSTALL_SECONDS" make install >/dev/null)
  # OCCT links fontconfig by bare name (CSF_fontconfig).
  occt_link=(-DCMAKE_SHARED_LINKER_FLAGS="-L$prefix/lib")
fi

log "RapidJSON $RAPIDJSON_COMMIT"
fetch "$work/rapidjson" https://github.com/Tencent/rapidjson.git "$RAPIDJSON_COMMIT" "$RAPIDJSON_COMMIT"
mkdir -p "$prefix/include"
bounded "$INSTALL_SECONDS" cp -R "$work/rapidjson/include/rapidjson" "$prefix/include/"

log "OCCT $OCCT_TAG"
fetch "$work/occt" https://github.com/Open-Cascade-SAS/OCCT.git "refs/tags/$OCCT_TAG" "$OCCT_COMMIT"
cmake_target "$work/occt" "$work/occt-build" "$OCCT_BUILD_SECONDS" \
  -DINSTALL_DIR="$prefix" -DINSTALL_DIR_LAYOUT=Unix -DINSTALL_DIR_WITH_VERSION=OFF \
  -DINSTALL_DIR_LIB=lib -DINSTALL_DIR_CMAKE=lib/cmake/opencascade -DBUILD_LIBRARY_TYPE=Shared \
  -DBUILD_MODULE_FoundationClasses=ON -DBUILD_MODULE_ModelingData=ON \
  -DBUILD_MODULE_ModelingAlgorithms=ON -DBUILD_MODULE_Visualization=ON \
  -DBUILD_MODULE_ApplicationFramework=ON -DBUILD_MODULE_DataExchange=ON \
  -DUSE_FREETYPE=ON -D3RDPARTY_FREETYPE_DIR="$prefix" \
  -D3RDPARTY_FREETYPE_INCLUDE_DIR_ft2build="$prefix/include/freetype2" \
  -D3RDPARTY_FREETYPE_INCLUDE_DIR_freetype2="$prefix/include/freetype2" \
  -D3RDPARTY_FREETYPE_LIBRARY="$prefix/lib/libfreetype.a" \
  -D3RDPARTY_FREETYPE_LIBRARY_DIR="$prefix/lib" \
  -DUSE_RAPIDJSON=ON -D3RDPARTY_RAPIDJSON_DIR="$prefix" \
  -D3RDPARTY_RAPIDJSON_INCLUDE_DIR="$prefix/include" \
  -DUSE_TBB=OFF -DUSE_TK=OFF -DUSE_XLIB=OFF -DUSE_OPENGL=OFF -DUSE_GLES2=OFF \
  -DUSE_FREEIMAGE=OFF -DUSE_FFMPEG=OFF -DUSE_OPENVR=OFF -DUSE_VTK=OFF -DUSE_DRACO=OFF \
  -DBUILD_MODULE_Draw=OFF -DBUILD_MODULE_DETools=OFF -DBUILD_DOC_Overview=OFF \
  ${occt_link[@]+"${occt_link[@]}"}
validate_sdk

rm -rf "$work"
{
  echo "target=$target"
  echo "occt=$OCCT_TAG $OCCT_COMMIT"
  echo "freetype=$FREETYPE_TAG $FREETYPE_COMMIT"
  echo "rapidjson=$RAPIDJSON_COMMIT"
  if [ "$target" = linux-aarch64 ]; then
    echo "expat=$EXPAT_TAG $EXPAT_COMMIT"
    echo "fontconfig=$FONTCONFIG_VERSION sha256:$FONTCONFIG_SHA256"
    echo "openssl=$OPENSSL_TAG $OPENSSL_COMMIT"
    echo "curl=$CURL_TAG $CURL_COMMIT"
  fi
} >"$prefix/.complete.tmp"
mv "$prefix/.complete.tmp" "$prefix/.complete"
log "built $prefix"
printf '%s\n' "$prefix" >&3
