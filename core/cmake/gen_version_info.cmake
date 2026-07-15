# Regenerate hostd_gen/version_info.h at BUILD time (cmake -P script mode),
# so AICAD_HOSTD_BUILD_ID tracks the working tree on every build — not the last
# CMake configure. The daemon_bridge version drain-replace compares the RUNNING
# daemon's id against `aicad-hostd --version` on disk; with a configure-time
# stamp both always matched after dev rebuilds, so a stale daemon could never
# be detected (mega-8 F7/F8: cycle-8 registry never reached the live engine).
#
# configure_file() only rewrites the header when content actually changed, so
# unchanged revs cost nothing (no relink storm).
#
# Inputs: -DSRC_DIR=<core source dir> -DOUT_HEADER=<generated header path>
execute_process(COMMAND git describe --tags --always --dirty
  WORKING_DIRECTORY ${SRC_DIR}
  OUTPUT_VARIABLE AICAD_HOSTD_BUILD_ID
  OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT AICAD_HOSTD_BUILD_ID)
  set(AICAD_HOSTD_BUILD_ID "unknown")
endif()
configure_file(${SRC_DIR}/hostd/version_info.h.in ${OUT_HEADER} @ONLY)
