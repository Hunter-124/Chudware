# ProvisionPolymesh.cmake — make the Polyhedral-FEA (`polymesh`) mesher + adaptive
# FEA stack available as in-tree static libraries.
#
# Polyhedral-FEA has no install/export set; Chudware consumes its static libraries through
# add_subdirectory and stages only the optional learned-advisor runtime.
#
# Dependency sharing is the whole reason this file exists rather than a bare
# add_subdirectory: polymesh's own top level would `find_package(Eigen3)` and pick up
# the SYSTEM Eigen, while core/ builds against the FetchContent Eigen 3.4 pinned above.
# Eigen types cross the static-library boundary (polymesh::fea's public API is
# Eigen::VectorXd), so two Eigen versions in one link is an ODR violation, not a
# warning. Upstream now skips its find_package when the host has already defined
# Eigen3::Eigen / nlohmann_json::nlohmann_json — which core/CMakeLists.txt does before
# including this module. Keep that ordering.
#
# LICENSE: BSD-3-Clause, statically linked.

# Pin an immutable commit on the single public Polyhedral-FEA line (master). There is no
# private fork: all polymesh work lands publicly, and CAD moves this pin to a master commit.
# Local maintenance trees opt in through POLYMESH_SOURCE_DIR (CHUDWARE_POLYMESH_ROOT).
set(POLYMESH_COMMIT "a9fe080daf6e15187ab9d31ce7acedb93d6d7fe7"
    CACHE STRING "Polyhedral-FEA commit to build" FORCE)
set(POLYMESH_REPOSITORY "https://github.com/Hunter-124/Polyhedral-FEA.git"
    CACHE STRING "Polyhedral-FEA fetch repository")

# Files this module WRITES into the managed checkout (audited transforms below).
# Git refuses to move HEAD over a locally modified tracked file, so without a
# targeted restore the first patch would block every future POLYMESH_COMMIT bump until
# someone cleaned the tree by hand. Keep this list in sync with anything that writes to
# ${_pm_src}: it is the exhaustive set of paths the checkout below is allowed to discard.
set(_pm_managed_patch_files
    "src/advisor/CMakeLists.txt"
    "src/adapt/src/graded_sizing.cpp")

# --- local development source override ---------------------------------------
# POLYMESH_SOURCE_DIR is a developer's live Polyhedral-FEA checkout (the repository root
# itself, i.e. the directory holding the top-level CMakeLists.txt — NOT a POLYMESH_ROOT
# whose src/ subdirectory is the checkout). build.sh forwards CHUDWARE_POLYMESH_ROOT here.
# Nothing is fetched, no pin is enforced and no file in it is patched: the whole point is
# that Chudware builds exactly what the developer has in the working tree.
set(POLYMESH_SOURCE_DIR "" CACHE PATH
    "Local Polyhedral-FEA checkout to build against (bypasses the pinned fetch)")
set(_pm_local_source FALSE)
if(POLYMESH_SOURCE_DIR)
  get_filename_component(_pm_local_source_dir "${POLYMESH_SOURCE_DIR}" ABSOLUTE)
  file(TO_CMAKE_PATH "${_pm_local_source_dir}" _pm_local_source_dir)
  set(_pm_local_source TRUE)
endif()

# A caller-supplied root is a deliberate local-source override: never fetch into it, check
# out another revision, or patch its files. The default third_party checkout is managed by
# this module and is kept at POLYMESH_COMMIT even when an older provision already exists.
set(_pm_default_root "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/polymesh")
get_filename_component(_pm_default_root "${_pm_default_root}" ABSOLUTE)
set(_pm_root_is_override FALSE)
if(DEFINED POLYMESH_ROOT)
  get_filename_component(_pm_configured_root "${POLYMESH_ROOT}" ABSOLUTE)
  if(NOT _pm_configured_root STREQUAL _pm_default_root)
    set(_pm_root_is_override TRUE)
  endif()
endif()
if(_pm_local_source)
  # The override IS the checkout; POLYMESH_ROOT is reported for diagnostics only, and the
  # fetch/patch machinery below is skipped exactly as it is for an explicit POLYMESH_ROOT.
  set(POLYMESH_ROOT "${_pm_local_source_dir}"
      CACHE PATH "Polyhedral-FEA source root (auto-provisioned if missing)" FORCE)
  set(_pm_root_is_override TRUE)
  set(_pm_src "${_pm_local_source_dir}")
else()
  set(POLYMESH_ROOT "${_pm_default_root}"
      CACHE PATH "Polyhedral-FEA source root (auto-provisioned if missing)")
  file(TO_CMAKE_PATH "${POLYMESH_ROOT}" POLYMESH_ROOT)
  set(_pm_src "${POLYMESH_ROOT}/src")
endif()

if(NOT _pm_root_is_override)
  find_package(Git REQUIRED)
  set(_pm_need_checkout TRUE)
  if(EXISTS "${_pm_src}/CMakeLists.txt" AND EXISTS "${_pm_src}/.git")
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${_pm_src}" rev-parse HEAD
      RESULT_VARIABLE _pm_head_rc
      OUTPUT_VARIABLE _pm_head
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET)
    if(_pm_head_rc EQUAL 0 AND _pm_head STREQUAL POLYMESH_COMMIT)
      set(_pm_need_checkout FALSE)
    endif()
  endif()

  if(_pm_need_checkout)
    file(MAKE_DIRECTORY "${_pm_src}")
    message(STATUS
      "[polymesh] fetching ${POLYMESH_REPOSITORY} @ ${POLYMESH_COMMIT} (LFS campaign data skipped)…")
    if(NOT EXISTS "${_pm_src}/.git")
      execute_process(
        COMMAND "${GIT_EXECUTABLE}" init -q "${_pm_src}"
        RESULT_VARIABLE _pm_rc)
    else()
      set(_pm_rc 0)
    endif()
    # Discard ONLY the files this module itself patched, so the checkout below can move
    # HEAD to a new pin. Anything else a developer changed in the managed tree is left
    # alone — and still fails the checkout loudly, which is the correct outcome for work
    # this module did not create. A path absent from the old HEAD is not an error here.
    if(_pm_rc EQUAL 0)
      foreach(_pm_patched IN LISTS _pm_managed_patch_files)
        execute_process(
          COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${_pm_src}" -c core.hooksPath= checkout -q -- "${_pm_patched}"
          RESULT_VARIABLE _pm_restore_rc OUTPUT_QUIET ERROR_QUIET)
      endforeach()
    endif()
    if(_pm_rc EQUAL 0)
      # Campaign archives are Git-LFS objects and are not build inputs. Some historical
      # objects are intentionally unavailable; skipping smudge leaves their pointer files
      # intact while the advisor's small ONNX/JSON artifacts check out normally.
      execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env GIT_LFS_SKIP_SMUDGE=1
                "${GIT_EXECUTABLE}" --no-replace-objects -C "${_pm_src}" -c http.sslVerify=true
                -c credential.helper= -c core.hooksPath= fetch --depth 1
                "${POLYMESH_REPOSITORY}" "${POLYMESH_COMMIT}"
        RESULT_VARIABLE _pm_rc)
    endif()
    if(_pm_rc EQUAL 0)
      execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env GIT_LFS_SKIP_SMUDGE=1
                "${GIT_EXECUTABLE}" --no-replace-objects -C "${_pm_src}" -c core.hooksPath=
                -c filter.lfs.smudge= -c filter.lfs.required=false checkout -q --detach FETCH_HEAD
        RESULT_VARIABLE _pm_rc)
    endif()
    if(NOT _pm_rc EQUAL 0)
      message(FATAL_ERROR
        "[polymesh] fetch/checkout of ${POLYMESH_COMMIT} from "
        "${POLYMESH_REPOSITORY} failed (${_pm_rc}); local changes were not removed")
    endif()
  endif()
  # Replacement refs can make a pinned commit name resolve to other objects, and
  # unmanaged worktree edits are not the pinned source. Only the module's own
  # managed patches may differ from authenticated HEAD.
  chudware_require_pinned_git("${_pm_src}" "${POLYMESH_COMMIT}")
  execute_process(
    COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${_pm_src}" status --porcelain=v1
            --untracked-files=all
    RESULT_VARIABLE _pm_status_rc OUTPUT_VARIABLE _pm_status OUTPUT_STRIP_TRAILING_WHITESPACE)
  foreach(_pm_patched IN LISTS _pm_managed_patch_files)
    string(REPLACE " M ${_pm_patched}" "" _pm_status "${_pm_status}")
  endforeach()
  string(STRIP "${_pm_status}" _pm_status)
  if(NOT _pm_status_rc EQUAL 0 OR _pm_status)
    message(FATAL_ERROR "[polymesh] managed checkout differs from pinned source: ${_pm_status}")
  endif()
  message(STATUS
    "Polyhedral-FEA: PINNED FETCH ${POLYMESH_REPOSITORY} @ ${POLYMESH_COMMIT}")
elseif(NOT EXISTS "${_pm_src}/CMakeLists.txt")
  message(FATAL_ERROR
    "[polymesh] source override has no CMakeLists.txt: ${_pm_src}")
elseif(_pm_local_source)
  # Record what the working tree actually is. A checkout with no git metadata, or a
  # rev-parse failure, is not an error — the developer may be building an export — but the
  # commit we report must then say so rather than claim the pin.
  find_package(Git QUIET)
  set(_pm_local_sha "unknown")
  if(GIT_EXECUTABLE)
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" -C "${_pm_src}" rev-parse HEAD
      RESULT_VARIABLE _pm_local_rc
      OUTPUT_VARIABLE _pm_local_head
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET)
    if(_pm_local_rc EQUAL 0 AND _pm_local_head)
      set(_pm_local_sha "${_pm_local_head}")
    endif()
  endif()
  set(POLYMESH_COMMIT "${_pm_local_sha}"
      CACHE STRING "Polyhedral-FEA commit to build" FORCE)
  message(STATUS "==============================================================")
  message(STATUS "Polyhedral-FEA: LOCAL SOURCE ${_pm_src} @ ${_pm_local_sha}")
  message(STATUS "==============================================================")
else()
  message(STATUS "[polymesh] using caller-supplied source override ${_pm_src}")
endif()

if(_pm_local_source)
  set(POLYMESH_MODEL_REVISION "local:${POLYMESH_COMMIT}")
elseif(_pm_root_is_override)
  set(POLYMESH_MODEL_REVISION "local-override")
else()
  set(POLYMESH_MODEL_REVISION "${POLYMESH_COMMIT}")
endif()

# --- advisor platform support -------------------------------------------------
# Upstream pins a prebuilt ONNX Runtime archive for exactly two platforms
# (src/advisor/CMakeLists.txt): Windows x64 and non-Apple 64-bit Unix. Everywhere else —
# macOS, or a 32-bit / other-pointer-width Unix — POLYMESH_WITH_ADVISOR=ON is a hard
# FATAL_ERROR raised inside the dependency, which took the whole CAD configure down with
# it. The mesher itself (polymesh::pipeline) has no such constraint, so mirror upstream's
# platform predicate here and build the pipeline WITHOUT the learned advisor rather than
# refusing to configure at all. Keep this condition identical to upstream's `_ort_archive`
# selection, narrowed to the x86-64 processors those two archives contain: upstream picks
# the linux-x64 archive for any 64-bit Unix, so an aarch64 Linux build would fetch x86-64
# ONNX Runtime and fail to link it. A build that satisfies this is exactly a build that can
# fetch and stage ORT.
if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|AMD64|amd64)$" AND
   ((WIN32 AND CMAKE_SIZEOF_VOID_P EQUAL 8) OR
    (UNIX AND NOT APPLE AND CMAKE_SIZEOF_VOID_P EQUAL 8)))
  set(_pm_advisor_enabled ON)
else()
  set(_pm_advisor_enabled OFF)
endif()
# CACHE INTERNAL because a CMake function body reads variables from its CALL site:
# chudware_polymesh_stage_runtime below is called from core/CMakeLists.txt, which also
# gates the advisor-linked backend object on this value.
set(POLYMESH_ADVISOR_ENABLED ${_pm_advisor_enabled}
    CACHE INTERNAL "Learned mesh advisor is buildable on this platform")
if(NOT POLYMESH_ADVISOR_ENABLED)
  message(STATUS
    "[polymesh] no prebuilt ONNX Runtime is pinned for ${CMAKE_SYSTEM_NAME}/"
    "${CMAKE_SYSTEM_PROCESSOR} (${CMAKE_SIZEOF_VOID_P}-byte pointers) — building the "
    "mesher without the learned advisor")
endif()

# Commit 9554579 pins the Windows ONNX Runtime archive but accidentally leaves the Linux
# digest empty. The official v1.28.0 release digest below turns the managed source into a
# deterministic derived input before add_subdirectory. Never silently download an
# unverified binary, and never mutate a caller's explicit local-source override.
#
# Idempotent by construction: the write happens only when the pinned line is absent, so a
# reconfigure is a no-op, and a future commit that ships the digest upstream needs no
# patch at all. A future commit that renames the pin still fails loudly rather than
# fetching an unverified archive.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_SIZEOF_VOID_P EQUAL 8)
  set(_pm_ort_hash
      "SHA256=a3e1b79d7bb1bf09696ce675f49e4064e6c81f6202b8225624fff0e93f8d6407")
  set(_pm_advisor_cmake "${_pm_src}/src/advisor/CMakeLists.txt")
  file(READ "${_pm_advisor_cmake}" _pm_advisor_text)
  string(FIND "${_pm_advisor_text}"
              "set(_ort_hash \"${_pm_ort_hash}\")"
              _pm_ort_hash_pos)
  if(_pm_ort_hash_pos EQUAL -1)
    if(_pm_root_is_override)
      message(FATAL_ERROR
        "[polymesh] explicit POLYMESH_ROOT does not pin the ONNX Runtime Linux archive "
        "to ${_pm_ort_hash}; update that local source rather than downloading unverified code")
    endif()
    string(REPLACE "set(_ort_hash \"\")"
                   "set(_ort_hash \"${_pm_ort_hash}\")"
                   _pm_advisor_pinned "${_pm_advisor_text}")
    if(_pm_advisor_pinned STREQUAL _pm_advisor_text)
      message(FATAL_ERROR
        "[polymesh] could not apply the audited ONNX Runtime digest to "
        "${_pm_advisor_cmake}; upstream layout changed")
    endif()
    file(WRITE "${_pm_advisor_cmake}" "${_pm_advisor_pinned}")
  endif()
endif()

# A constant GradedSizing already returns h_min without querying its sources.
# Upstream nevertheless builds the hash grid first: coplanar interface seeds
# have zero volume and h_min == h_max gives zero influence radius, producing
# enormous/overflowing grid dimensions and std::bad_alloc on Apple silicon.
# Apply the same constant-field predicate before grid construction. Both the
# pinned original and exact derived source are authenticated, making repeated
# configure a no-op without accepting arbitrary managed-source modifications.
# Explicit source overrides remain the caller's tree and are never patched.
if(NOT _pm_root_is_override)
  set(_pm_graded_source "${_pm_src}/src/adapt/src/graded_sizing.cpp")
  set(_pm_graded_original_hash
      "046ffbb3c17317362200acf7eaf11b042d674eae652bac7abf1ff5d82f98be55")
  set(_pm_graded_corrected_hash
      "6467537f25b5024eb7e0d3339dde013733eb668884c95e80f11938fe1f11854a")
  file(SHA256 "${_pm_graded_source}" _pm_graded_hash)
  if(_pm_graded_hash STREQUAL _pm_graded_original_hash)
    file(READ "${_pm_graded_source}" _pm_graded_text)
    string(REPLACE
      "    grid_nx_ = grid_ny_ = grid_nz_ = 0;\n    if (sources_.empty()) {"
      "    grid_nx_ = grid_ny_ = grid_nz_ = 0;\n    if (sources_.empty() || h_max_ - h_min_ <= 1e-30) {"
      _pm_graded_corrected "${_pm_graded_text}")
    string(SHA256 _pm_graded_derived_hash "${_pm_graded_corrected}")
    if(NOT _pm_graded_derived_hash STREQUAL _pm_graded_corrected_hash)
      message(FATAL_ERROR
        "[polymesh] constant-field grid correction did not produce the audited source")
    endif()
    file(WRITE "${_pm_graded_source}" "${_pm_graded_corrected}")
  elseif(NOT _pm_graded_hash STREQUAL _pm_graded_corrected_hash)
    message(FATAL_ERROR
      "[polymesh] graded sizing source differs from the pinned original and audited "
      "constant-field correction; update the managed transform for the new upstream source")
  endif()
endif()

# --- embed configuration -----------------------------------------------------
set(POLYMESH_BUILD_TESTS  OFF CACHE BOOL "" FORCE)
set(POLYMESH_WITH_GUI     OFF CACHE BOOL "" FORCE)
set(POLYMESH_WITH_ADVISOR ${POLYMESH_ADVISOR_ENABLED} CACHE BOOL "" FORCE)
set(POLYMESH_WITH_CUDA    OFF CACHE BOOL "" FORCE)
# Chudware consumes the graded-tet family only; the restricted CVT/Geogram kernel cannot
# cross the fixed-stride tet contract and is intentionally not built.
set(POLYMESH_WITH_GEOGRAM OFF CACHE BOOL "" FORCE)
# polymesh compiles itself -Werror under -Wconversion. That is its own CI's business;
# a dependency must never be able to fail OUR build on a new compiler's new warning.
set(POLYMESH_WERROR       OFF CACHE BOOL "" FORCE)
# STEP/BREP support uses the same OpenCASCADE build as Chudware.
set(POLYMESH_WITH_OCC     ON  CACHE BOOL "" FORCE)
if(DEFINED OpenCASCADE_DIR)
  set(OpenCASCADE_DIR "${OpenCASCADE_DIR}" CACHE PATH "" FORCE)
endif()

# --- advisor runtime artifacts -----------------------------------------------
# What pm::advisor::Advisor actually OPENS: the graph plus the three required sidecars
# (normalization, clamp/candidate box, OOD veto) — ~170 KB in total. The rest of
# bench/advisor is campaign evidence, the cross-validation dumps alone near 90 MB, and
# has no runtime role; staging the directory wholesale would ship a research corpus.
# CACHE INTERNAL because a CMake function body reads variables from its CALL site, and
# chudware_polymesh_stage_runtime below is called from core/CMakeLists.txt.
set(POLYMESH_ADVISOR_MODEL_FILES "model.onnx;normalization.json;clamps.json;ood.json"
    CACHE INTERNAL "Advisor artifacts required at runtime")
# Developer fallback model directory compiled into sim/mesher.cpp. Packaged installs use
# the staged package beside libchudware instead.
set(POLYMESH_ADVISOR_SOURCE_DIR "${_pm_src}/bench/advisor"
    CACHE INTERNAL "Advisor model source directory (developer fallback)")
# Name of the staged package directory, resolved at runtime relative to the module that
# loads the advisor. ONE spelling, shared by the staging function and the C++ resolver.
set(POLYMESH_ADVISOR_PACKAGE_DIR "polymesh-advisor"
    CACHE INTERNAL "Staged advisor package directory name")
if(POLYMESH_ADVISOR_ENABLED)
  foreach(_pm_model_file IN LISTS POLYMESH_ADVISOR_MODEL_FILES)
    if(NOT EXISTS "${POLYMESH_ADVISOR_SOURCE_DIR}/${_pm_model_file}")
      message(FATAL_ERROR
        "[polymesh] advisor artifact ${_pm_model_file} is missing from "
        "${POLYMESH_ADVISOR_SOURCE_DIR} at ${POLYMESH_MODEL_REVISION}; the advisor cannot "
        "load without it, so fail here rather than ship a build whose advisor is dead")
    endif()
  endforeach()
endif()

add_subdirectory("${_pm_src}" "${CMAKE_CURRENT_BINARY_DIR}/third_party/polymesh" EXCLUDE_FROM_ALL)

if(NOT TARGET polymesh::pipeline)
  message(FATAL_ERROR
    "[polymesh] pipeline target missing after add_subdirectory at pinned commit "
    "${POLYMESH_COMMIT}")
endif()
if(POLYMESH_ADVISOR_ENABLED AND NOT TARGET polymesh::advisor)
  message(FATAL_ERROR
    "[polymesh] advisor target missing after add_subdirectory at pinned commit "
    "${POLYMESH_COMMIT} despite POLYMESH_WITH_ADVISOR=ON")
endif()

if(POLYMESH_ADVISOR_ENABLED)
  set(_pm_advisor_note "advisor")
else()
  set(_pm_advisor_note "no advisor")
endif()
message(STATUS
  "[polymesh] ${POLYMESH_COMMIT}  src=${_pm_src} (BSD-3-Clause, static, ${_pm_advisor_note})")

# --- staged ONNX Runtime file name -------------------------------------------
# ELF records the SONAME of the object it linked, never the path or the link name it was
# reached through. The release archive ships lib/libonnxruntime.so as a two-hop symlink
# (…so -> …so.1 -> …so.1.28.0) onto an object whose SONAME is libonnxruntime.so.1, so
# libchudware.so comes out carrying DT_NEEDED libonnxruntime.so.1 while the old staging
# copied that link's contents beside it as plain "libonnxruntime.so" — a name the loader
# never asks for. Resolve the chain to the real object and read the SONAME out of its
# ELF, so the staged file is named exactly the string the loader looks for in $ORIGIN.
# Guessing (dropping a suffix, or trusting the intermediate link name) would silently
# ship an engine that cannot dlopen, so an unreadable SONAME fails the configure.
# Windows has no SONAME: onnxruntime.dll is bound by name through its import library.
if(POLYMESH_ADVISOR_ENABLED)
  if(NOT POLYMESH_ONNXRUNTIME_RUNTIME)
    message(FATAL_ERROR
      "[polymesh] POLYMESH_ONNXRUNTIME_RUNTIME is unset even though polymesh::advisor was "
      "configured with POLYMESH_WITH_ADVISOR=ON; upstream layout changed")
  endif()
  get_filename_component(_pm_ort_real "${POLYMESH_ONNXRUNTIME_RUNTIME}" REALPATH)
  if(WIN32)
    get_filename_component(_pm_ort_staged_name "${_pm_ort_real}" NAME)
  else()
    set(_pm_ort_soname "")
    # LC_ALL=C: binutils field labels are translated, and this parses them.
    if(CMAKE_READELF)
      execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env LC_ALL=C "${CMAKE_READELF}" -d "${_pm_ort_real}"
        OUTPUT_VARIABLE _pm_ort_dynamic
        ERROR_QUIET
        RESULT_VARIABLE _pm_ort_readelf_rc)
      if(_pm_ort_readelf_rc EQUAL 0)
        string(REGEX MATCH "Library soname: \\[([A-Za-z0-9._+-]+)\\]"
               _pm_ort_soname_match "${_pm_ort_dynamic}")
        set(_pm_ort_soname "${CMAKE_MATCH_1}")
      endif()
    endif()
    if(NOT _pm_ort_soname AND CMAKE_OBJDUMP)
      execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env LC_ALL=C "${CMAKE_OBJDUMP}" -p "${_pm_ort_real}"
        OUTPUT_VARIABLE _pm_ort_dynamic
        ERROR_QUIET
        RESULT_VARIABLE _pm_ort_objdump_rc)
      if(_pm_ort_objdump_rc EQUAL 0)
        string(REGEX MATCH "SONAME[ \t]+([A-Za-z0-9._+-]+)"
               _pm_ort_soname_match "${_pm_ort_dynamic}")
        set(_pm_ort_soname "${CMAKE_MATCH_1}")
      endif()
    endif()
    if(NOT _pm_ort_soname)
      message(FATAL_ERROR
        "[polymesh] could not read the SONAME of ${_pm_ort_real} with readelf or objdump. "
        "The ONNX Runtime has to be staged under the exact name the linker records as "
        "DT_NEEDED in libchudware, so install binutils or reconfigure with "
        "-DCHUDWARE_WITH_POLYMESH=OFF rather than ship an engine that cannot load")
    endif()
    set(_pm_ort_staged_name "${_pm_ort_soname}")
  endif()
  # CACHE INTERNAL for the same call-site reason as the model variables above.
  set(POLYMESH_ONNXRUNTIME_STAGE_FILE "${_pm_ort_real}"
      CACHE INTERNAL "Real ONNX Runtime object to stage (symlink chain resolved)")
  set(POLYMESH_ONNXRUNTIME_STAGE_NAME "${_pm_ort_staged_name}"
      CACHE INTERNAL "File name the loader resolves the ONNX Runtime under")
  message(STATUS "[polymesh] ONNX Runtime stages as ${POLYMESH_ONNXRUNTIME_STAGE_NAME}")
endif()

# Stage everything an advisor-linked binary needs AT RUNTIME beside that binary.
#
# Upstream's own polymesh_stage_onnxruntime is WIN32-only, so on Linux the ONLY thing
# resolving libonnxruntime.so was the build-tree `_deps` path that CMake bakes into the
# link — an absolute path to a directory a release does not contain. Copying it next to
# the consumer and adding $ORIGIN makes the same binary start from a staged tree.
#
# The model package rides in `polymesh-advisor` beside the engine; sim/mesher.cpp resolves
# it relative to its own module. Call this for every target that links the advisor.
function(chudware_polymesh_stage_runtime target)
  if(NOT POLYMESH_ADVISOR_ENABLED)
    # No prebuilt ONNX Runtime is pinned for this platform, so ${target} links no advisor
    # and there is nothing beside it to resolve. The mesher still builds: a no-op, not an
    # error, or every consumer would have to repeat the platform predicate.
    return()
  endif()
  if(NOT POLYMESH_ONNXRUNTIME_STAGE_FILE OR NOT POLYMESH_ONNXRUNTIME_STAGE_NAME)
    message(FATAL_ERROR
      "[polymesh] the ONNX Runtime staging name was never resolved — polymesh::advisor was "
      "configured without a prebuilt ONNX Runtime, so ${target} would link a library it "
      "cannot stage")
  endif()
  # The destination NAME is load-bearing: the loader searches $ORIGIN for the SONAME
  # (libonnxruntime.so.1), not for the archive's libonnxruntime.so symlink name.
  add_custom_command(TARGET ${target} POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${POLYMESH_ONNXRUNTIME_STAGE_FILE}"
            "$<TARGET_FILE_DIR:${target}>/${POLYMESH_ONNXRUNTIME_STAGE_NAME}"
    COMMENT "Staging the ONNX Runtime + advisor model beside ${target}"
    VERBATIM)
  foreach(_pm_model_file IN LISTS POLYMESH_ADVISOR_MODEL_FILES)
    add_custom_command(TARGET ${target} POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different
              "${POLYMESH_ADVISOR_SOURCE_DIR}/${_pm_model_file}"
              "$<TARGET_FILE_DIR:${target}>/${POLYMESH_ADVISOR_PACKAGE_DIR}/${_pm_model_file}"
      VERBATIM)
  endforeach()
  if(NOT WIN32)
    # The staged runtime sits next to the consumer, so $ORIGIN covers build and install.
    set_property(TARGET ${target} APPEND PROPERTY BUILD_RPATH "$ORIGIN")
    set_property(TARGET ${target} APPEND PROPERTY INSTALL_RPATH "$ORIGIN")
  endif()
endfunction()
