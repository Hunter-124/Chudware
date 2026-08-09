"""Permanent named refusals for robot execution, transport, and safety surfaces."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReservedMethodRefusal(RuntimeError):
    method_name: str
    reason: str

    def __str__(self) -> str:
        return f"{self.method_name}: {self.reason}"


RESERVED_METHOD_NAMES = (
    "mfg_robot_jog",
    "mfg_robot_home",
    "mfg_robot_enable",
    "mfg_robot_servo_on",
    "mfg_robot_program_load",
    "mfg_robot_program_start",
    "mfg_robot_stop",
    "mfg_robot_reset",
    "mfg_robot_connect",
    "mfg_robot_stream",
)

RESERVED_REFUSALS = {
    name: "reserved_robot_method_permanently_unimplemented: offline planning has no execute, transport, or safety path"
    for name in RESERVED_METHOD_NAMES
}


def refuse_reserved_method(method_name: str) -> None:
    """Refuse a reserved name while keeping it absent from every dispatch table."""
    reason = RESERVED_REFUSALS.get(method_name)
    if reason is None:
        raise KeyError(f"{method_name}: not a reserved robot method name")
    raise ReservedMethodRefusal(method_name, reason)
