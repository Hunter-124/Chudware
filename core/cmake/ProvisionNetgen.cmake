# ProvisionNetgen.cmake — make the Netgen / nglib tet mesher available.
#
# Mirrors ProvisionOCCT.cmake's auto-provisioning philosophy, but Netgen has no official
# prebuilt Windows SDK, so we BUILD IT FROM SOURCE (once, at configure time): clone a pinned
# tag, apply a small double-free fix, configure it WITHOUT GUI/Python/MPI and WITH OpenCascade
# pointed at OUR provisioned OCCT, then `--build --target INSTALL`. The result is cached under
# third_party/netgen/install and reused on every later configure.
#
# Building against OUR OCCT 7.9.3 is REQUIRED, not incidental: nglib.dll imports the OCCT
# toolkit DLLs (TKernel.dll, ...) by name, so it MUST share the same OCCT libaicad.dll loads —
# two different OCCT builds with identical DLL names would collide in-process. Sharing one OCCT
# also lets Ng_OCC_Load_STEP read the STEP with the same kernel (no two-OCCT clash).
#
# LICENSE NOTE: Netgen/nglib is LGPL-2.1. We link it DYNAMICALLY (nglib.dll + ngcore.dll
# shipped next to libaicad.dll) and offer the Netgen source, which satisfies the LGPL. This
# replaced gmsh (GPL-2+, no linking exception) — the #1 commercialization blocker.

include(ExternalProject)  # not used directly, but keeps the module self-describing

set(NETGEN_VERSION "v6.2.2504" CACHE STRING "Netgen git tag to build")
set(NETGEN_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/netgen"
    CACHE PATH "Netgen build root (auto-provisioned if missing)")
file(TO_CMAKE_PATH "${NETGEN_ROOT}" NETGEN_ROOT)

set(_ng_src     "${NETGEN_ROOT}/src")
set(_ng_build   "${NETGEN_ROOT}/build")
set(_ng_install "${NETGEN_ROOT}/install")
# nglib link target: a Windows import lib, or the .so on ELF (lib or lib64 — re-globbed
# after the build to get the real path). A nonexistent default just triggers the build.
if(WIN32)
  set(_ng_implib "${_ng_install}/lib/nglib.lib")
else()
  file(GLOB _ng_implib_glob "${_ng_install}/lib/libnglib.so" "${_ng_install}/lib64/libnglib.so")
  if(_ng_implib_glob)
    list(GET _ng_implib_glob 0 _ng_implib)
  else()
    set(_ng_implib "${_ng_install}/lib/libnglib.so")
  endif()
endif()

# --- locate OUR OCCT (provisioned by ProvisionOCCT, included just before this) + its zlib ----
# OpenCASCADE_DIR / OCCT_ROOT are set by ProvisionOCCT. Netgen finds OCC via
# find_package(OpenCascade) (respects OpenCascade_DIR) and zlib via find_package(ZLIB).
if(WIN32)
  if(NOT OCCT_ROOT)
    message(FATAL_ERROR "[netgen] OCCT_ROOT not set — include(ProvisionOCCT) before ProvisionNetgen")
  endif()
  set(_occt_cmake "${OCCT_ROOT}/opencascade-${OCCT_VERSION}-vc14-64/cmake")
  file(GLOB _ng_zlib_dir "${OCCT_ROOT}/3rdparty-vc14-64/zlib-*-vc14-64")
  if(NOT _ng_zlib_dir)
    message(FATAL_ERROR "[netgen] zlib not found under ${OCCT_ROOT}/3rdparty-vc14-64 (OCCT 3rdparty)")
  endif()
  list(GET _ng_zlib_dir 0 _ng_zlib_dir)
else()
  # Linux/macOS: build against the SYSTEM OCCT (OpenCASCADE_DIR set by ProvisionOCCT's
  # find_package); Netgen finds the system zlib itself via find_package(ZLIB).
  if(NOT OpenCASCADE_DIR)
    message(FATAL_ERROR "[netgen] OpenCASCADE_DIR not set — include(ProvisionOCCT) before ProvisionNetgen")
  endif()
  set(_occt_cmake "${OpenCASCADE_DIR}")
endif()

# --- build from source if the install isn't already present ------------------
if(NOT EXISTS "${_ng_implib}")
  find_package(Git REQUIRED)
  file(MAKE_DIRECTORY "${NETGEN_ROOT}")

  # 1) shallow clone the pinned tag (no submodules needed: USE_PYTHON=OFF).
  if(NOT EXISTS "${_ng_src}/CMakeLists.txt")
    message(STATUS "[netgen] cloning Netgen ${NETGEN_VERSION} (from source — no prebuilt SDK)…")
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" clone --depth 1 --branch "${NETGEN_VERSION}"
              https://github.com/NGSolve/netgen.git "${_ng_src}"
      RESULT_VARIABLE _ng_clone_rc)
    if(NOT _ng_clone_rc EQUAL 0)
      message(FATAL_ERROR "[netgen] git clone failed (${_ng_clone_rc})")
    endif()
  endif()

  # 2) patch the double-free in Mesh::DeleteMesh: it frees bcnames/cd2names but does not
  #    resize the arrays, so the ~Mesh destructor (which runs after DeleteMesh on
  #    Ng_DeleteMesh) deletes the same pointers again -> heap corruption that KILLS the
  #    process. OCC meshing populates bcnames, so this fires on every mesh. Clearing the
  #    arrays right after the frees fixes it. Idempotent (guarded on the marker).
  set(_ng_meshclass "${_ng_src}/libsrc/meshing/meshclass.cpp")
  file(READ "${_ng_meshclass}" _ng_mc)
  if(NOT _ng_mc MATCHES "AI-CAD patch")
    string(REPLACE
      "    for (int i= 0; i< cd2names.Size(); i++)\n      if (cd2names[i]) delete cd2names[i];\n"
      "    for (int i= 0; i< cd2names.Size(); i++)\n      if (cd2names[i]) delete cd2names[i];\n    // AI-CAD patch: clear the freed name arrays so ~Mesh cannot delete these pointers a\n    // second time (double-free that killed the process on Ng_DeleteMesh).\n    bcnames.SetSize(0);\n    cd2names.SetSize(0);\n"
      _ng_mc_new "${_ng_mc}")
    if(_ng_mc_new STREQUAL _ng_mc)
      message(FATAL_ERROR "[netgen] could not apply the DeleteMesh double-free patch — "
                          "meshclass.cpp layout changed for tag ${NETGEN_VERSION}?")
    endif()
    file(WRITE "${_ng_meshclass}" "${_ng_mc_new}")
    message(STATUS "[netgen] applied Mesh::DeleteMesh double-free patch")
  endif()

  # 3) configure: no GUI/Python/MPI, OCC ON against our OCCT, zlib from OCCT 3rdparty, shared
  #    libs (LGPL dynamic link), non-native arch (portable dist). Same generator as the parent.
  set(_ng_gen_args -G "${CMAKE_GENERATOR}")
  if(CMAKE_GENERATOR_PLATFORM)
    list(APPEND _ng_gen_args -A "${CMAKE_GENERATOR_PLATFORM}")
  endif()
  # Windows pins zlib to OCCT's 3rdparty copy; on ELF let Netgen find the system zlib.
  set(_ng_zlib_args "")
  if(WIN32)
    set(_ng_zlib_args "-DZLIB_INCLUDE_DIR=${_ng_zlib_dir}/include"
                      "-DZLIB_LIBRARY=${_ng_zlib_dir}/lib/zlib.lib")
  endif()
  message(STATUS "[netgen] configuring (from source against OCCT @ ${_occt_cmake})…")
  execute_process(
    COMMAND "${CMAKE_COMMAND}" -S "${_ng_src}" -B "${_ng_build}" ${_ng_gen_args}
            "-DCMAKE_INSTALL_PREFIX=${_ng_install}"
            "-DCMAKE_BUILD_TYPE=Release"          # single-config generators ignore --config
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"  # Netgen carries pre-3.5 cmake_minimum_required
            "-DCMAKE_PREFIX_PATH=${_occt_cmake}"
            "-DOpenCascade_DIR=${_occt_cmake}"
            "-DOpenCASCADE_DIR=${_occt_cmake}"
            ${_ng_zlib_args}
            -DUSE_SUPERBUILD=OFF -DUSE_GUI=OFF -DUSE_PYTHON=OFF -DUSE_OCC=ON
            -DUSE_MPI=OFF -DUSE_NATIVE_ARCH=OFF -DBUILD_STUB_FILES=OFF
    RESULT_VARIABLE _ng_cfg_rc)
  if(NOT _ng_cfg_rc EQUAL 0)
    message(FATAL_ERROR "[netgen] configure failed (${_ng_cfg_rc})")
  endif()

  # 4) build + install (Release). This compiles Netgen once (a few minutes) and caches it.
  message(STATUS "[netgen] building (one-time; cached under ${_ng_install})…")
  if(WIN32)
    set(_ng_install_target INSTALL)   # the VS-generator install target is upper-case
  else()
    set(_ng_install_target install)
  endif()
  execute_process(
    COMMAND "${CMAKE_COMMAND}" --build "${_ng_build}" --config Release --target ${_ng_install_target} --parallel
    RESULT_VARIABLE _ng_build_rc)
  if(NOT _ng_build_rc EQUAL 0)
    message(FATAL_ERROR "[netgen] build failed (${_ng_build_rc})")
  endif()
endif()
# Re-resolve the nglib path on ELF now that the install exists (lib vs lib64).
if(NOT WIN32)
  file(GLOB _ng_implib_glob "${_ng_install}/lib/libnglib.so" "${_ng_install}/lib64/libnglib.so")
  if(_ng_implib_glob)
    list(GET _ng_implib_glob 0 _ng_implib)
  endif()
endif()
if(NOT EXISTS "${_ng_implib}")
  message(FATAL_ERROR "[netgen] expected ${_ng_implib} after provisioning — build layout changed?")
endif()

# --- expose locations --------------------------------------------------------
set(NETGEN_INCLUDE_DIRS "${_ng_install}/include" CACHE PATH "Netgen include dir" FORCE)
set(NETGEN_IMPORT_LIB   "${_ng_implib}"          CACHE FILEPATH "nglib import lib" FORCE)

# Runtime shared libs to stage next to libaicad: nglib + ngcore. On Windows also stage
# zlib.dll (nglib's dep, which OCCT's DLL staging does NOT copy); on ELF the system zlib
# resolves via the loader, so NETGEN_ZLIB_DLL stays empty.
if(WIN32)
  file(GLOB _ng_dlls "${_ng_install}/bin/*.dll")
  set(NETGEN_DLLS "${_ng_dlls}" CACHE STRING "Netgen runtime DLLs" FORCE)
  set(NETGEN_ZLIB_DLL "${_ng_zlib_dir}/bin/zlib.dll" CACHE FILEPATH "zlib DLL (nglib dep)" FORCE)
else()
  file(GLOB _ng_dlls "${_ng_install}/lib/libng*.so*" "${_ng_install}/lib64/libng*.so*")
  set(NETGEN_DLLS "${_ng_dlls}" CACHE STRING "Netgen runtime shared libs" FORCE)
  set(NETGEN_ZLIB_DLL "" CACHE FILEPATH "zlib DLL (nglib dep; unused on ELF)" FORCE)
endif()

message(STATUS "[netgen] ${NETGEN_VERSION}  include=${NETGEN_INCLUDE_DIRS}")
message(STATUS "[netgen]   import lib=${NETGEN_IMPORT_LIB}")
message(STATUS "[netgen]   runtime dlls=${NETGEN_DLLS} + ${NETGEN_ZLIB_DLL}")

# Deploy the Netgen runtime DLLs (+ zlib) next to a target's binary so libaicad.dll's import
# of nglib.dll resolves when it is loaded from the same directory.
function(aicad_netgen_copy_dlls tgt)
  foreach(_d ${NETGEN_DLLS} ${NETGEN_ZLIB_DLL})
    if(_d AND EXISTS "${_d}")
      add_custom_command(TARGET ${tgt} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_d}" "$<TARGET_FILE_DIR:${tgt}>"
        COMMENT "[netgen] copying ${_d} next to ${tgt}")
    endif()
  endforeach()
endfunction()
