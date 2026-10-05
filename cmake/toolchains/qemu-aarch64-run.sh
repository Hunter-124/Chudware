#!/bin/sh
# CMAKE_CROSSCOMPILING_EMULATOR of aarch64-linux-gnu.cmake:
#   qemu-aarch64-run.sh QEMU SYSROOT PROGRAM [ARGS...]
# runs one aarch64 program under qemu-user.
#
# QEMU_LD_PREFIX points qemu, and through the environment every aarch64 child
# the program starts via binfmt_misc, at the cross sysroot's loader and libc.
#
# Each run gets its own TMPDIR, removed afterwards. Emulation is slow enough
# that the suite runs ctest in parallel, and several tests write fixed file
# names (chudware_polyfea_beam.step, ...) into std::filesystem::
# temp_directory_path(); natively they run one at a time, here they must not
# overwrite each other. Parent and children of one test share its directory.
set -eu
qemu=$1
sysroot=$2
shift 2
run_tmp=$(mktemp -d "${TMPDIR:-/tmp}/chudware-qemu.XXXXXXXX")
trap 'rm -rf "$run_tmp"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
QEMU_LD_PREFIX=$sysroot
TMPDIR=$run_tmp
export QEMU_LD_PREFIX TMPDIR
status=0
"$qemu" "$@" || status=$?
exit "$status"
