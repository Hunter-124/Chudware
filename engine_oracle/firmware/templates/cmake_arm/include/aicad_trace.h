/* aicad_trace.h — firmware-side pin-event shim for the QEMU emulation fallback (T-FIN1).
 *
 * QEMU (mps2-an385) has no external GPIO observer the host can poll, so the firmware
 * reports its own pin transitions over the semihosting console in a machine-readable
 * line the host-side parser (engine/firmware/emulator.py) picks up:
 *
 *     AICAD_GPIO_EVT <PIN>=<level> t=<ms>ms
 *
 * Usage (semihosting must be enabled — fw_sim_run's QEMU path passes -semihosting):
 *
 *     #include "aicad_trace.h"
 *     AICAD_GPIO_TRACE("PA5", 1, millis());   // pin name, level (0/1), timestamp in ms
 *     AICAD_GPIO_TRACE_NT("PA5", 0);          // no-timestamp variant
 *
 * The same line format is what Renode's generated state-change hooks print, so
 * assertions ("gpio:PA5=1", timeline "gpio:PA5=1@>10ms") work identically on both
 * backends. Plain printf is enough: the mps2-an385 semihosting console is stdout.
 */
#ifndef AICAD_TRACE_H
#define AICAD_TRACE_H

#include <stdio.h>

#define AICAD_GPIO_TRACE(pin_str, level, t_ms) \
    printf("AICAD_GPIO_EVT %s=%d t=%lums\n", (pin_str), ((level) ? 1 : 0), \
           (unsigned long)(t_ms))

#define AICAD_GPIO_TRACE_NT(pin_str, level) \
    printf("AICAD_GPIO_EVT %s=%d\n", (pin_str), ((level) ? 1 : 0))

#endif /* AICAD_TRACE_H */
